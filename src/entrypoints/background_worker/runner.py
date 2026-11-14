"""Точка входа фонового воркера регулярного сбора, нормализации и проверки источников афиши."""

import argparse
import asyncio
import contextlib
import signal
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any
from uuid import uuid4

from src.application.event_ingestion import EventIngestionService
from src.core.exceptions import ApplicationException, LifecycleClientNotInitializedError, ValidationException
from src.core.logger import get_logger, reset_request_identifier, set_request_identifier
from src.domain.entities import IngestionReport, NotificationDispatchReport
from src.domain.ports import RawNormalizationStatus
from src.entrypoints.bootstrap import ApplicationContainer, build_application_container
from src.infrastructure.sources import load_sources_registry

__all__ = ["BackgroundWorkerRunner", "build_argument_parser", "main", "run_background_worker"]

logger = get_logger(__name__)

_REPROCESS_STATUSES: tuple[RawNormalizationStatus, ...] = ("pending", "failed")


class BackgroundWorkerRunner:
    """Управляемый планировщик и исполнитель прогонов фонового сбора данных.

    Отвечает за загрузку активного реестра источников, запуск конвейера
    `EventIngestionService`, опциональное переигрывание сырых записей,
    периодическую проверку доступности опубликованных ссылок и изоляцию
    прикладных ошибок между итерациями демона.
    """

    def __init__(
        self,
        container: ApplicationContainer,
        *,
        sources_config_path: Path | None = None,
        poll_interval_seconds: float | None = None,
        verify_links_on_run: bool = False,
        reprocess_on_run: bool = False,
    ) -> None:
        """Инициализирует исполнитель фонового воркера поверх контейнера зависимостей.

        Arguments:
            container: Собранный контейнер зависимостей приложения.
            sources_config_path: Переопределение пути к YAML-реестру источников.
            poll_interval_seconds: Переопределение интервала между циклами опроса в секундах.
            verify_links_on_run: Проверять ли доступность ссылок опубликованных карточек на каждом прогоне.
            reprocess_on_run: Переигрывать ли сырые записи в статусах `pending` и `failed` на каждом прогоне.

        Raises:
            ValidationException: Если передан неположительный интервал опроса.
        """
        resolved_interval = (
            float(poll_interval_seconds)
            if poll_interval_seconds is not None
            else float(container.ingestion_settings.POLL_INTERVAL_SECONDS)
        )
        if resolved_interval <= 0.0:
            raise ValidationException("Интервал опроса фонового воркера должен быть положительным числом.")

        self._container = container
        self._sources_config_path = sources_config_path or container.ingestion_settings.SOURCES_CONFIG_PATH
        self._poll_interval_seconds = resolved_interval
        self._verify_links_on_run = verify_links_on_run
        self._reprocess_on_run = reprocess_on_run

    @property
    def sources_config_path(self) -> Path:
        """Возвращает путь к используемому YAML-реестру источников."""
        return self._sources_config_path

    @property
    def poll_interval_seconds(self) -> float:
        """Возвращает интервал ожидания между циклами опроса в секундах."""
        return self._poll_interval_seconds

    def _require_ingestion_service(self) -> EventIngestionService:
        """Возвращает инициализированный сервис сбора событий из контейнера зависимостей."""
        service = self._container.event_ingestion_service
        if service is None:
            raise LifecycleClientNotInitializedError(
                "Сервис сбора событий EventIngestionService не инициализирован в контейнере."
            )
        return service

    async def run_once(
        self, *, reprocess: bool = False, verify_links: bool = False, current_time: datetime | None = None
    ) -> IngestionReport:
        """Выполняет один полный цикл опроса активных источников и дополнительных проверок.

        Arguments:
            reprocess: Принудительно запустить переигрывание сырых записей `pending` и `failed`.
            verify_links: Принудительно запустить проверку доступности ссылок опубликованных событий.
            current_time: Необязательная опорная метка времени для детерминированных тестов и прогонов.

        Returns:
            Сводный отчёт `IngestionReport` по итогам опроса активных источников.

        Raises:
            ApplicationException: При сбое инициализации, чтения реестра источников или критической ошибке сервиса.
        """
        ingestion_service = self._require_ingestion_service()
        sources = load_sources_registry(self._sources_config_path, only_enabled=True)
        sources_by_name = {source.name: source for source in sources}

        report = await ingestion_service.ingest_sources(sources=sources, current_time=current_time)
        _log_ingestion_report(stage="ingest_sources", report=report)

        if reprocess or self._reprocess_on_run:
            batch_limit = self._container.ingestion_settings.REPROCESS_BATCH_LIMIT
            for status in _REPROCESS_STATUSES:
                reprocess_report = await ingestion_service.reprocess_raw_events(
                    normalization_status=status,
                    limit=batch_limit,
                    current_time=current_time,
                    sources_by_name=sources_by_name,
                )
                _log_ingestion_report(stage=f"reprocess:{status}", report=reprocess_report)

        if verify_links or self._verify_links_on_run:
            demoted_count = await ingestion_service.verify_published_links(current_time=current_time)
            logger.info("Проверка ссылок опубликованных карточек завершена: снято_в_черновик=%s.", demoted_count)

        subscription_service = getattr(self._container, "subscription_service", None)
        if subscription_service is not None:
            try:
                dispatch_report = await subscription_service.dispatch_pending_telegram_notifications(
                    current_time=current_time
                )
                _log_notification_dispatch_report(report=dispatch_report)
            except Exception as error:
                logger.warning("Ошибка фоновой рассылки Telegram-уведомлений по подпискам: %s", error)

        return report

    async def run_forever(
        self, *, stop_event: asyncio.Event | None = None, reprocess: bool = False, verify_links: bool = False
    ) -> None:
        """Запускает периодический цикл фонового сбора с изоляцией сбоев и управляемой остановкой.

        Arguments:
            stop_event: Асинхронное событие завершения работы цикла.
            reprocess: Переигрывать ли сырые записи `pending` и `failed` на каждой итерации.
            verify_links: Проверять ли доступность ссылок опубликованных событий на каждой итерации.
        """
        effective_stop_event = stop_event or asyncio.Event()
        logger.info(
            "Запуск фонового воркера в периодическом режиме (интервал=%.1f сек., реестр=%s).",
            self._poll_interval_seconds,
            self._sources_config_path,
        )

        while not effective_stop_event.is_set():
            iteration_token = set_request_identifier(f"worker-{uuid4().hex[:12]}")
            try:
                await self.run_once(reprocess=reprocess, verify_links=verify_links)
            except ApplicationException as error:
                logger.exception("Ошибка итерации фонового воркера; ожидание следующего цикла: %s", error)
            finally:
                reset_request_identifier(iteration_token)

            if effective_stop_event.is_set():
                break

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(effective_stop_event.wait(), timeout=self._poll_interval_seconds)

        logger.info("Периодический цикл фонового воркера корректно остановлен.")


def _log_ingestion_report(*, stage: str, report: IngestionReport) -> None:
    """Записывает структурированную сводку `IngestionReport` в журнал приложения."""
    logger.info(
        "Отчёт этапа '%s': sources_processed=%s, sources_failed=%s, items_fetched=%s, "
        "items_published=%s, items_sent_to_moderation=%s, items_deduplicated=%s, "
        "items_unchanged=%s, errors_count=%s.",
        stage,
        report.sources_processed,
        report.sources_failed,
        report.items_fetched,
        report.items_published,
        report.items_sent_to_moderation,
        report.items_deduplicated,
        report.items_unchanged,
        len(report.errors),
    )
    for error_message in report.errors:
        logger.warning("Диагностика этапа '%s': %s", stage, error_message)


def _log_notification_dispatch_report(*, report: NotificationDispatchReport) -> None:
    """Записывает сводку рассылки Telegram-уведомлений по подпискам в журнал приложения."""
    logger.info(
        "Рассылка Telegram-уведомлений по подпискам: attempted=%s, sent=%s, deferred=%s, failed=%s.",
        report.attempted_count,
        report.sent_count,
        report.deferred_count,
        report.failed_count,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки фонового воркера.

    Returns:
        Настроенный экземпляр `argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.entrypoints.background_worker.runner",
        description="Фоновый воркер сбора, нормализации и проверки источников афиши.",
    )
    parser.add_argument("--once", action="store_true", help="Выполнить один цикл сбора источников и завершить процесс.")
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Дополнительно переиграть нормализацию сырых записей в статусах pending и failed.",
    )
    parser.add_argument(
        "--verify-links",
        action="store_true",
        help="Дополнительно проверить доступность ссылок ранее опубликованных мероприятий.",
    )
    return parser


async def run_background_worker(
    *,
    container: ApplicationContainer | None = None,
    stop_event: asyncio.Event | None = None,
    once: bool = False,
    reprocess: bool = False,
    verify_links: bool = False,
    manage_lifecycle: bool = True,
) -> int:
    """Инициализирует жизненный цикл контейнера, обрабатывает сигналы и запускает воркер.

    Arguments:
        container: Необязательный готовый контейнер зависимостей (удобен для тестов).
        stop_event: Необязательное внешнее событие остановки для управляемого завершения.
        once: Выполнить только один прогон вместо бесконечного периодического цикла.
        reprocess: Переигрывать ли сырые записи в статусах `pending` и `failed`.
        verify_links: Проверять ли доступность ссылок опубликованных карточек.
        manage_lifecycle: Вызывать ли `startup()` и `shutdown()` у контейнера зависимостей.

    Returns:
        Код завершения процесса (`0` при успехе, `1` при критическом сбое).
    """
    resolved_container = container or build_application_container()
    effective_stop_event = stop_event or asyncio.Event()
    restore_signals = _install_stop_signal_handlers(effective_stop_event)

    try:
        if manage_lifecycle:
            await resolved_container.startup()

        runner = BackgroundWorkerRunner(resolved_container)
        if once:
            report = await runner.run_once(reprocess=reprocess, verify_links=verify_links)
            if report.sources_failed > 0 and report.sources_processed == 0:
                logger.error(
                    "Однократный запуск завершился критическим отказом всех источников (sources_failed=%s).",
                    report.sources_failed,
                )
                return 1
            return 0

        await runner.run_forever(stop_event=effective_stop_event, reprocess=reprocess, verify_links=verify_links)
        return 0
    except ApplicationException as error:
        logger.error("Критическая ошибка фонового воркера: %s", error)
        return 1
    finally:
        restore_signals()
        if manage_lifecycle:
            await resolved_container.shutdown()


def _install_stop_signal_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    """Подключает обработчики `SIGINT` и `SIGTERM` для корректного завершения ожидания."""
    running_loop = asyncio.get_running_loop()
    registered_loop_signals: list[signal.Signals] = []
    previous_handlers: dict[signal.Signals, Any] = {}

    for target_signal in _supported_stop_signals():
        try:
            running_loop.add_signal_handler(target_signal, stop_event.set)
            registered_loop_signals.append(target_signal)
        except NotImplementedError:
            if threading.current_thread() is threading.main_thread():
                previous_handlers[target_signal] = signal.getsignal(target_signal)

                def _handle_signal(_signum: int, _frame: FrameType | None) -> None:
                    running_loop.call_soon_threadsafe(stop_event.set)

                signal.signal(target_signal, _handle_signal)

    def _restore() -> None:
        for registered_signal in registered_loop_signals:
            running_loop.remove_signal_handler(registered_signal)
        for target_signal, previous_handler in previous_handlers.items():
            signal.signal(target_signal, previous_handler)

    return _restore


def _supported_stop_signals() -> tuple[signal.Signals, ...]:
    """Возвращает кортеж поддерживаемых текущей платформой сигналов завершения."""
    signals: list[signal.Signals] = [signal.SIGINT]
    sigterm = getattr(signal, "SIGTERM", None)
    if isinstance(sigterm, signal.Signals):
        signals.append(sigterm)
    return tuple(signals)


def main(argv: Sequence[str] | None = None) -> int:
    """Синхронная точка запуска процесса `python -m src.entrypoints.background_worker.runner`.

    Arguments:
        argv: Необязательный список аргументов командной строки (по умолчанию используется `sys.argv[1:]`).

    Returns:
        Целочисленный код возврата процесса (`0` или `1`).
    """
    parser = build_argument_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return asyncio.run(
            run_background_worker(
                once=bool(arguments.once),
                reprocess=bool(arguments.reprocess),
                verify_links=bool(arguments.verify_links),
            )
        )
    except KeyboardInterrupt:
        logger.info("Остановка фонового воркера по сигналу прерывания.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

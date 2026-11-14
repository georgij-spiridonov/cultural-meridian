"""Точка входа запуска Telegram-бота на `aiogram` v3 в режиме long polling."""

import asyncio
import contextlib
import signal
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any, Final

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from src.core.logger import get_logger
from src.entrypoints.bootstrap import ApplicationContainer, build_application_container
from src.entrypoints.telegram_bot.handlers import create_telegram_router

__all__ = ["build_bot_commands", "build_telegram_dispatcher", "main", "run_idle_wait_loop", "run_telegram_bot"]

logger = get_logger(__name__)

_BOT_COMMANDS: Final[tuple[BotCommand, ...]] = (
    BotCommand(command="start", description="Главное меню и быстрые фильтры"),
    BotCommand(command="help", description="Справка по командам и поиску"),
    BotCommand(command="plan", description="Планировщик маршрута «Выходной день»"),
    BotCommand(command="subscribe", description="Подписка на город и категорию"),
    BotCommand(command="unsubscribe", description="Управление и удаление подписок"),
    BotCommand(command="cancel", description="Сбросить шаг или отменить подтверждение"),
)


def build_bot_commands() -> tuple[BotCommand, ...]:
    """Возвращает стандартный перечень команд Telegram-бота для регистрации в API."""
    return _BOT_COMMANDS


def build_telegram_dispatcher(container: ApplicationContainer) -> Dispatcher:
    """Собирает диспетчер `aiogram` v3 и подключает маршрутизатор бота.

    Arguments:
        container: Собранный контейнер зависимостей приложения.

    Returns:
        Настроенный экземпляр `Dispatcher`.
    """
    dispatcher = Dispatcher()
    dispatcher.include_router(create_telegram_router(container))
    return dispatcher


async def run_idle_wait_loop(*, stop_event: asyncio.Event, idle_poll_seconds: float) -> None:
    """Безопасно ожидает сигнала остановки при отсутствии токена Telegram-бота.

    Arguments:
        stop_event: Асинхронное событие завершения работы процесса.
        idle_poll_seconds: Интервал пробуждения цикла ожидания в секундах.
    """
    while not stop_event.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=idle_poll_seconds)


async def run_telegram_bot(
    *,
    container: ApplicationContainer | None = None,
    stop_event: asyncio.Event | None = None,
    manage_lifecycle: bool = True,
) -> None:
    """Запускает Telegram-бота в режиме long polling либо в безопасном режиме ожидания.

    Arguments:
        container: Опциональный готовый контейнер зависимостей (удобен для тестов).
        stop_event: Необязательное внешнее событие остановки для тестов и управляемого завершения.
        manage_lifecycle: Вызывать ли `startup()` и `shutdown()` у контейнера зависимостей.
    """
    resolved_container = container or build_application_container()
    effective_stop_event = stop_event or asyncio.Event()

    if manage_lifecycle:
        await resolved_container.startup()

    restore_signals = _install_stop_signal_handlers(effective_stop_event)
    try:
        bot_token = resolved_container.telegram_settings.BOT_TOKEN.strip()
        if not bot_token:
            logger.warning(
                "Переменная TELEGRAM_BOT_TOKEN не задана. Telegram-бот запущен в безопасном режиме ожидания "
                "без подключения к API Telegram (интервал опроса остановки: %.1f сек.).",
                resolved_container.telegram_settings.IDLE_POLL_SECONDS,
            )
            await run_idle_wait_loop(
                stop_event=effective_stop_event,
                idle_poll_seconds=resolved_container.telegram_settings.IDLE_POLL_SECONDS,
            )
            return

        dispatcher = build_telegram_dispatcher(resolved_container)
        bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await bot.set_my_commands(list(build_bot_commands()))
            logger.info("Команды Telegram-бота зарегистрированы; запуск long polling.")
            stop_watcher = asyncio.create_task(_watch_stop_event(effective_stop_event, dispatcher))
            try:
                await dispatcher.start_polling(
                    bot, allowed_updates=dispatcher.resolve_used_update_types(), handle_signals=False
                )
            finally:
                stop_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_watcher
        finally:
            await bot.session.close()
    finally:
        restore_signals()
        if manage_lifecycle:
            await resolved_container.shutdown()


async def _watch_stop_event(stop_event: asyncio.Event, dispatcher: Dispatcher) -> None:
    await stop_event.wait()
    await dispatcher.stop_polling()


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
    signals: list[signal.Signals] = [signal.SIGINT]
    sigterm = getattr(signal, "SIGTERM", None)
    if isinstance(sigterm, signal.Signals):
        signals.append(sigterm)
    return tuple(signals)


def main() -> None:
    """Синхронная точка запуска процесса `python -m src.entrypoints.telegram_bot.runner`."""
    try:
        asyncio.run(run_telegram_bot())
    except KeyboardInterrupt:
        logger.info("Остановка Telegram-бота по сигналу прерывания.")


if __name__ == "__main__":
    main()

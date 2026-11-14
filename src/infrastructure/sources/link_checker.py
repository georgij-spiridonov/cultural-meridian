"""Адаптер проверки доступности HTTP(S)-ссылок и локальных файлов источников."""

import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from src.core.configuration import PROJECT_ROOT
from src.core.exceptions import (
    AccessPermissionDeniedError,
    ApplicationException,
    NetworkConnectionTimeoutError,
    ServiceRateLimitExceededError,
    ServiceUnavailableError,
    ValidationInputError,
)
from src.domain.entities import LinkCheckResult
from src.domain.exceptions import UnsupportedSourceFormatError
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.infrastructure.sources.http_client import PoliteHttpClient
from src.infrastructure.sources.registry import DEFAULT_SOURCE_USER_AGENT, is_http_url, resolve_local_source_path

__all__ = ["HttpLinkChecker"]

# 403 добавлен к фоллбэку: часть сайтов отвечает «Forbidden» на HEAD, но отдаёт страницу по GET.
_HEAD_FALLBACK_STATUS_CODES: frozenset[int] = frozenset({403, 405, 501})


class HttpLinkChecker:
    """Асинхронная проверка доступности URL и локальных фидов без падения конвейера."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = DEFAULT_SOURCE_USER_AGENT,
        default_timeout_seconds: float = 4.0,
        project_root: Path = PROJECT_ROOT,
        local_feeds_directory: Path | None = None,
        verify_local_file_links: bool = True,
        polite_client: PoliteHttpClient | None = None,
    ) -> None:
        """Инициализирует адаптер проверки ссылок с безопасной обработкой локальных путей.

        Arguments:
            http_client: Внешний экземпляр `httpx.AsyncClient` (не закрывается при `close()`).
            transport: Опциональный транспорт `httpx` для изолированного тестирования.
            user_agent: Значение HTTP-заголовка `User-Agent`.
            default_timeout_seconds: Таймаут по умолчанию в секундах при некорректном аргументе.
            project_root: Корневая директория проекта для контроля локальных путей.
            local_feeds_directory: Дополнительная директория локальных фидов.
            verify_local_file_links: Проверять ли физическое наличие локальных файлов на диске.
            polite_client: Общий вежливый HTTP-клиент сборщика; без него создаётся собственный.
        """
        self._owns_polite_client = polite_client is None
        self._polite_client = polite_client or PoliteHttpClient(
            user_agent=user_agent,
            request_delay_seconds=0.0,
            maximum_attempts=1,
            http_client=http_client,
            transport=transport,
        )
        self._default_timeout_seconds = default_timeout_seconds
        self._project_root = project_root
        self._local_feeds_directory = local_feeds_directory
        self._verify_local_file_links = verify_local_file_links

    async def close(self) -> None:
        """Закрывает собственный HTTP-клиент, если он был создан самим адаптером."""
        if self._owns_polite_client:
            await self._polite_client.close()

    async def _check_http_link(
        self, *, target_url: str, timeout_seconds: float, checked_at: datetime
    ) -> LinkCheckResult:
        """Выполняет HTTP HEAD (с фоллбэком на GET при 405/501) без выброса исключений."""
        parsed = urlsplit(target_url)
        if not parsed.netloc:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason="Некорректный HTTP-адрес ссылки источника.",
                checked_at=checked_at,
            )

        effective_timeout = timeout_seconds if timeout_seconds > 0.0 else self._default_timeout_seconds

        try:
            response = await self._polite_client.request("HEAD", target_url, timeout_seconds=effective_timeout)
            if response.status_code in _HEAD_FALLBACK_STATUS_CODES:
                response = await self._polite_client.request("GET", target_url, timeout_seconds=effective_timeout)
        except NetworkConnectionTimeoutError:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason="Превышен тайм-аут проверки ссылки источника.",
                checked_at=checked_at,
            )
        except (ServiceRateLimitExceededError, ServiceUnavailableError) as exc:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason=f"Сервер источника недоступен: {exc.message}",
                checked_at=checked_at,
            )
        except ApplicationException as exc:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason=f"Сетевая ошибка при проверке ссылки: {exc.message}",
                checked_at=checked_at,
            )

        status_code = response.status_code
        final_url = str(response.url) if response.url is not None else target_url
        if 200 <= status_code < 400:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=True,
                http_status_code=status_code,
                final_url=final_url,
                error_reason=None,
                checked_at=checked_at,
            )

        return LinkCheckResult(
            target_url=target_url,
            is_reachable=False,
            http_status_code=status_code,
            final_url=final_url,
            error_reason=f"Сервер вернул HTTP {status_code}",
            checked_at=checked_at,
        )

    async def _check_local_link(self, *, target_url: str, checked_at: datetime) -> LinkCheckResult:
        """Проверяет существование локального файла источника внутри разрешённых директорий."""
        if not self._verify_local_file_links:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=True,
                http_status_code=200,
                final_url=target_url,
                error_reason=None,
                checked_at=checked_at,
            )

        try:
            resolved_path = resolve_local_source_path(
                target_url, project_root=self._project_root, local_feeds_directory=self._local_feeds_directory
            )
        except AccessPermissionDeniedError as exc:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=403,
                final_url=None,
                error_reason=exc.message,
                checked_at=checked_at,
            )
        except (UnsupportedSourceFormatError, ValidationInputError) as exc:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason=exc.message,
                checked_at=checked_at,
            )

        exists_as_file = await asyncio.to_thread(resolved_path.is_file)
        if exists_as_file:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=True,
                http_status_code=200,
                final_url=target_url,
                error_reason=None,
                checked_at=checked_at,
            )

        return LinkCheckResult(
            target_url=target_url,
            is_reachable=False,
            http_status_code=404,
            final_url=None,
            error_reason="Локальный файл источника не найден",
            checked_at=checked_at,
        )

    async def check_link(self, *, target_url: str, timeout_seconds: float = 4.0) -> LinkCheckResult:
        """Проверяет доступность HTTP(S)-ссылки или локального пути источника без выброса исключений.

        Arguments:
            target_url: Проверяемый URL или путь к локальному файлу.
            timeout_seconds: Максимальное время ожидания сетевого ответа в секундах.

        Returns:
            Структурированный результат проверки `LinkCheckResult`.
        """
        checked_at = datetime.now(tz=BASHKORTOSTAN_TIMEZONE)
        cleaned_url = target_url.strip()
        if not cleaned_url:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason="Пустой URL ссылки источника.",
                checked_at=checked_at,
            )

        if is_http_url(cleaned_url):
            return await self._check_http_link(
                target_url=cleaned_url, timeout_seconds=timeout_seconds, checked_at=checked_at
            )

        return await self._check_local_link(target_url=cleaned_url, checked_at=checked_at)

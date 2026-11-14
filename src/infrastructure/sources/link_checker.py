"""Адаптер проверки доступности HTTP(S)-ссылок и локальных файлов источников."""

import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from src.core.configuration import PROJECT_ROOT
from src.core.exceptions import AccessPermissionDeniedError, ValidationInputError
from src.domain.entities import LinkCheckResult
from src.domain.exceptions import UnsupportedSourceFormatError
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.infrastructure.sources.registry import DEFAULT_SOURCE_USER_AGENT, is_http_url, resolve_local_source_path

__all__ = ["HttpLinkChecker"]

_HEAD_FALLBACK_STATUS_CODES: frozenset[int] = frozenset({405, 501})


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
        """
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._transport = transport
        self._user_agent = user_agent
        self._default_timeout_seconds = default_timeout_seconds
        self._project_root = project_root
        self._local_feeds_directory = local_feeds_directory
        self._verify_local_file_links = verify_local_file_links

    def _get_or_create_http_client(self) -> httpx.AsyncClient:
        """Возвращает переданный или лениво созданный асинхронный HTTP-клиент."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._default_timeout_seconds),
                headers={"User-Agent": self._user_agent},
                follow_redirects=True,
                transport=self._transport,
            )
            self._owns_http_client = True
        return self._http_client

    async def close(self) -> None:
        """Закрывает внутренний `httpx.AsyncClient`, если он был создан самим адаптером."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

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
        headers = {"User-Agent": self._user_agent}
        client = self._get_or_create_http_client()

        try:
            response = await client.head(target_url, timeout=effective_timeout, headers=headers, follow_redirects=True)
            if response.status_code in _HEAD_FALLBACK_STATUS_CODES:
                response = await client.get(
                    target_url, timeout=effective_timeout, headers=headers, follow_redirects=True
                )
        except httpx.TimeoutException:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason="Превышен тайм-аут проверки ссылки источника.",
                checked_at=checked_at,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason=f"Сетевая ошибка при проверке ссылки: {exc}",
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

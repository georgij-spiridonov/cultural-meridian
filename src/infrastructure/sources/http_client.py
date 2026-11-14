"""Вежливый HTTP-клиент сборщика афиш: robots.txt, User-Agent, ограничение параллелизма, паузы и повторы."""

import asyncio
from typing import Final
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from src.core.exceptions import (
    AccessEndpointInvalidError,
    AccessPermissionDeniedError,
    NetworkConnectionFailureError,
    NetworkConnectionTimeoutError,
    NetworkDataTransferError,
    ServiceRateLimitExceededError,
    ServiceUnavailableError,
    ValidationInputError,
)
from src.core.logger import get_logger

__all__ = ["PoliteHttpClient"]

logger = get_logger(__name__)

_DEFAULT_REQUEST_DELAY_SECONDS: Final[float] = 1.0
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
_MAXIMUM_ATTEMPTS: Final[int] = 3
_RETRY_BASE_DELAY_SECONDS: Final[float] = 1.0
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
_ROBOTS_TIMEOUT_SECONDS: Final[float] = 10.0
_DISALLOW_ALL_RULES: Final[tuple[str, ...]] = ("User-agent: *", "Disallow: /")


def _extract_origin(url: str) -> tuple[str, str]:
    """Выделяет схему с хостом и сам хост из URL.

    Arguments:
        url: Абсолютный HTTP(S)-адрес.

    Returns:
        Кортеж `(origin, host)`, например `("https://bashgf.ru", "bashgf.ru")`.

    Raises:
        AccessEndpointInvalidError: Если адрес не является абсолютным HTTP(S)-адресом.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise AccessEndpointInvalidError(f"Некорректный адрес источника: {url}")
    return f"{parts.scheme}://{parts.netloc}", parts.netloc.lower()


class PoliteHttpClient:
    """HTTP-клиент, который соблюдает правила вежливого обхода сайтов источников.

    Соблюдает `robots.txt`, отправляет собственный `User-Agent`, держит не больше заданного числа
    одновременных запросов к одному хосту, выдерживает паузу между запросами и повторяет
    запрос с нарастающей паузой при сетевых сбоях и ответах 429/5xx. Один экземпляр делится
    между всеми загрузчиками и проверкой ссылок, чтобы ограничения действовали на сайт целиком.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        max_concurrent_requests_per_host: int = 2,
        request_delay_seconds: float | None = None,
        maximum_attempts: int = _MAXIMUM_ATTEMPTS,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Инициализирует клиент.

        Arguments:
            user_agent: Значение HTTP-заголовка `User-Agent`.
            max_concurrent_requests_per_host: Максимальное число одновременных запросов к одному хосту.
            request_delay_seconds: Пауза между началами запросов к одному хосту; по умолчанию 1 с.
            maximum_attempts: Число попыток запроса при сетевых сбоях и ответах 429/5xx (первая и повторы).
            http_client: Внешний `httpx.AsyncClient` (не закрывается при `close()`).
            transport: Транспорт `httpx` для изолированного тестирования.
        """
        self._user_agent = user_agent
        self._max_concurrent_requests_per_host = max_concurrent_requests_per_host
        self._request_delay_seconds = request_delay_seconds
        self._maximum_attempts = max(1, maximum_attempts)
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._transport = transport
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self._host_pacing_locks: dict[str, asyncio.Lock] = {}
        self._host_last_request_at: dict[str, float] = {}
        self._robots_parsers: dict[str, RobotFileParser] = {}
        self._robots_lock = asyncio.Lock()

    async def close(self) -> None:
        """Закрывает внутренний `httpx.AsyncClient`, если он был создан самим клиентом."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def fetch_text(self, url: str, *, timeout_seconds: float) -> str:
        """Загружает текст страницы с соблюдением `robots.txt`.

        Arguments:
            url: Адрес страницы.
            timeout_seconds: Таймаут одного запроса в секундах.

        Returns:
            Текст ответа.

        Raises:
            AccessPermissionDeniedError: Если `robots.txt` запрещает обход адреса.
            ValidationInputError: Если сервер ответил ошибкой 4xx, кроме 429.
            NetworkException: При сетевом сбое после всех повторов.
            ServiceAvailabilityException: При ответе 429/5xx после всех повторов.
        """
        if not await self.is_allowed(url):
            raise AccessPermissionDeniedError(f"robots.txt запрещает обход адреса: {url}")
        response = await self.request("GET", url, timeout_seconds=timeout_seconds)
        if response.status_code >= 400:
            raise ValidationInputError(f"Страница ответила кодом {response.status_code}: {url}")
        return response.text

    async def is_allowed(self, url: str) -> bool:
        """Проверяет, разрешает ли `robots.txt` сайта обход адреса нашим `User-Agent`.

        Arguments:
            url: Проверяемый адрес.

        Returns:
            `True`, если обход разрешён.
        """
        origin, _ = _extract_origin(url)
        parser = await self._get_robots_parser(origin)
        return parser.can_fetch(self._user_agent, url)

    async def request(self, method: str, url: str, *, timeout_seconds: float) -> httpx.Response:
        """Выполняет запрос с ограничением параллелизма, паузой и повторами, без проверки `robots.txt`.

        Ответы 4xx, кроме 429, возвращаются вызывающему коду без исключения.

        Arguments:
            method: HTTP-метод (`GET`, `HEAD`).
            url: Адрес запроса.
            timeout_seconds: Таймаут одного запроса в секундах.

        Returns:
            Ответ сервера.

        Raises:
            ValidationInputError: Если адрес не удаётся разобрать.
            NetworkConnectionTimeoutError: При превышении таймаута после всех повторов.
            NetworkConnectionFailureError: При ошибке соединения после всех повторов.
            NetworkDataTransferError: При прочих транспортных сбоях после всех повторов.
            ServiceRateLimitExceededError: При ответе 429 после всех повторов.
            ServiceUnavailableError: При ответе 5xx после всех повторов.
        """
        _, host = _extract_origin(url)
        semaphore = self._host_semaphores.setdefault(host, asyncio.Semaphore(self._max_concurrent_requests_per_host))
        for attempt in range(1, self._maximum_attempts + 1):
            is_last_attempt = attempt == self._maximum_attempts
            async with semaphore:
                await self._wait_for_host_slot(host)
                try:
                    response = await self._get_or_create_http_client().request(
                        method, url, timeout=httpx.Timeout(timeout_seconds)
                    )
                except httpx.InvalidURL as error:
                    raise ValidationInputError(f"Некорректный URL: {url} ({error})") from error
                except httpx.TimeoutException as error:
                    if is_last_attempt:
                        raise NetworkConnectionTimeoutError(f"Превышен таймаут запроса: {url}") from error
                except httpx.ConnectError as error:
                    if is_last_attempt:
                        raise NetworkConnectionFailureError(f"Не удалось подключиться: {url}") from error
                except httpx.HTTPError as error:
                    if is_last_attempt:
                        raise NetworkDataTransferError(f"Сбой передачи данных: {url}") from error
                else:
                    if response.status_code not in _RETRYABLE_STATUS_CODES:
                        return response
                    if is_last_attempt:
                        if response.status_code == 429:
                            raise ServiceRateLimitExceededError(f"Источник ограничил частоту запросов: {url}")
                        raise ServiceUnavailableError(f"Источник ответил кодом {response.status_code}: {url}")
            retry_delay = _RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1)
            logger.warning("Повтор запроса %s %s через %.1f с (попытка %d).", method, url, retry_delay, attempt + 1)
            await asyncio.sleep(retry_delay)
        raise AssertionError("Недостижимая ветка цикла повторов.")

    async def _wait_for_host_slot(self, host: str) -> None:
        """Выдерживает паузу между началами запросов к одному хосту."""
        delay = self._request_delay_seconds
        if delay is None:
            delay = _DEFAULT_REQUEST_DELAY_SECONDS
        lock = self._host_pacing_locks.setdefault(host, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            last_request_at = self._host_last_request_at.get(host)
            if last_request_at is not None:
                remaining = delay - (loop.time() - last_request_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._host_last_request_at[host] = loop.time()

    async def _get_robots_parser(self, origin: str) -> RobotFileParser:
        """Загружает и кэширует правила `robots.txt` сайта.

        Отсутствующий файл (4xx) разрешает обход, ответ 401/403 запрещает его. При сетевом сбое
        или ответе 5xx обход разрешается с предупреждением в лог, чтобы временный сбой
        `robots.txt` не останавливал сбор. HTML вместо `robots.txt` (перенаправление на главную)
        не содержит директив и поэтому разрешает обход.
        """
        async with self._robots_lock:
            cached = self._robots_parsers.get(origin)
            if cached is not None:
                return cached

            parser = RobotFileParser(f"{origin}/robots.txt")
            robots_lines: list[str] = []
            try:
                response = await self.request("GET", f"{origin}/robots.txt", timeout_seconds=_ROBOTS_TIMEOUT_SECONDS)
            except (NetworkConnectionTimeoutError, NetworkConnectionFailureError, NetworkDataTransferError) as error:
                logger.warning("robots.txt сайта %s недоступен (%s), обход разрешён.", origin, error)
            except (ServiceRateLimitExceededError, ServiceUnavailableError) as error:
                logger.warning("robots.txt сайта %s отвечает ошибкой (%s), обход разрешён.", origin, error)
            else:
                if response.status_code in {401, 403}:
                    robots_lines = list(_DISALLOW_ALL_RULES)
                elif response.status_code < 400:
                    robots_lines = response.text.splitlines()
            parser.parse(robots_lines)
            self._robots_parsers[origin] = parser
            return parser

    def _get_or_create_http_client(self) -> httpx.AsyncClient:
        """Возвращает внешний клиент или лениво создаёт собственный."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(_DEFAULT_TIMEOUT_SECONDS),
                headers={"User-Agent": self._user_agent},
                follow_redirects=True,
                transport=self._transport,
            )
            self._owns_http_client = True
        return self._http_client

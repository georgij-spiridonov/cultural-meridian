"""Промежуточные обработчики HTTP-запросов: контекст, лимиты, метрики и заголовки безопасности."""

import re
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from uuid import uuid4

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from src.core.logger import get_logger, reset_request_identifier, set_request_identifier

__all__ = ["InMemoryRateLimiter", "MetricsSnapshot", "RequestContextMiddleware", "RequestMetricsCollector"]

logger = get_logger(__name__)

_REQUEST_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_LATENCY_WINDOW_SIZE = 1000
_RATE_LIMIT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsSnapshot:
    """Агрегированный срез метрик наблюдаемости HTTP-сервиса.

    Attributes:
        requests_total: Общее число обработанных HTTP-запросов.
        errors_4xx: Количество ответов с клиентскими ошибками (`4xx`).
        errors_5xx: Количество ответов с серверными ошибками (`5xx`).
        average_latency_milliseconds: Среднее время ответа на последних запросах в миллисекундах.
        p95_latency_milliseconds: 95-й процентиль времени ответа в миллисекундах.
    """

    requests_total: int
    errors_4xx: int
    errors_5xx: int
    average_latency_milliseconds: float
    p95_latency_milliseconds: float


class RequestMetricsCollector:
    """Потокобезопасный сборщик базовых метрик задержки, запросов и ошибок в памяти процесса."""

    def __init__(self) -> None:
        """Инициализирует счётчики запросов, ошибок и буфер задержек."""
        self._lock = Lock()
        self._requests_total = 0
        self._errors_4xx = 0
        self._errors_5xx = 0
        self._latencies_ms: deque[float] = deque(maxlen=_LATENCY_WINDOW_SIZE)

    def record_request(self, *, status_code: int, duration_milliseconds: float) -> None:
        """Регистрирует завершённый HTTP-запрос и его длительность."""
        with self._lock:
            self._requests_total += 1
            if 400 <= status_code < 500:
                self._errors_4xx += 1
            elif status_code >= 500:
                self._errors_5xx += 1
            self._latencies_ms.append(max(0.0, duration_milliseconds))

    def snapshot(self) -> MetricsSnapshot:
        """Возвращает консистентный срез текущих метрик сервиса."""
        with self._lock:
            samples = sorted(self._latencies_ms)
            requests_total = self._requests_total
            errors_4xx = self._errors_4xx
            errors_5xx = self._errors_5xx

        if not samples:
            return MetricsSnapshot(
                requests_total=requests_total,
                errors_4xx=errors_4xx,
                errors_5xx=errors_5xx,
                average_latency_milliseconds=0.0,
                p95_latency_milliseconds=0.0,
            )

        average_ms = sum(samples) / len(samples)
        p95_index = max(0, min(len(samples) - 1, int(0.95 * (len(samples) - 1))))
        return MetricsSnapshot(
            requests_total=requests_total,
            errors_4xx=errors_4xx,
            errors_5xx=errors_5xx,
            average_latency_milliseconds=round(average_ms, 2),
            p95_latency_milliseconds=round(samples[p95_index], 2),
        )


class InMemoryRateLimiter:
    """Ограничитель частоты запросов со скользящим окном по IP-адресу и области действия."""

    def __init__(self, *, general_limit_per_minute: int, feedback_limit_per_minute: int) -> None:
        """Инициализирует лимиты запросов в минуту для общего контура и формы обратной связи.

        Arguments:
            general_limit_per_minute: Общий порог запросов с одного адреса за 60 секунд.
            feedback_limit_per_minute: Порог отправок формы обратной связи за 60 секунд.
        """
        self._general_limit = general_limit_per_minute
        self._feedback_limit = feedback_limit_per_minute
        self._lock = Lock()
        self._buckets: dict[tuple[str, str], deque[float]] = {}

    def check_and_consume(self, *, client_address: str, scope: str) -> tuple[bool, int]:
        """Проверяет лимит и фиксирует обращение.

        Arguments:
            client_address: Нормализованный адрес клиента.
            scope: Контур ограничения (`general` или `feedback`).

        Returns:
            Кортеж `(allowed, retry_after_seconds)`.
        """
        limit = self._feedback_limit if scope == "feedback" else self._general_limit
        now = time.monotonic()
        cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
        bucket_key = (scope, client_address)

        with self._lock:
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                bucket = deque()
                self._buckets[bucket_key] = bucket

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + _RATE_LIMIT_WINDOW_SECONDS - now) + 1)
                return False, retry_after

            bucket.append(now)
            if len(self._buckets) > 4096:
                stale_keys = [
                    key for key, timestamps in self._buckets.items() if not timestamps or timestamps[-1] <= cutoff
                ]
                for stale_key in stale_keys:
                    del self._buckets[stale_key]
            return True, 0


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Промежуточный слой сквозной трассировки `request_id`, ограничений частоты и безопасности."""

    def __init__(
        self, application: ASGIApp, *, metrics_collector: RequestMetricsCollector, rate_limiter: InMemoryRateLimiter
    ) -> None:
        """Настраивает middleware поверх FastAPI-приложения.

        Arguments:
            application: Оборачиваемое ASGI-приложение.
            metrics_collector: Сборщик базовых метрик задержки и ошибок.
            rate_limiter: Ограничитель частоты запросов в памяти процесса.
        """
        super().__init__(application)
        self._metrics_collector = metrics_collector
        self._rate_limiter = rate_limiter

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Обрабатывает входящий HTTP-запрос, измеряет время и добавляет заголовки защиты."""
        raw_header_id = request.headers.get("X-Request-ID", "").strip()
        request_identifier = raw_header_id if _REQUEST_IDENTIFIER_PATTERN.match(raw_header_id) else uuid4().hex[:16]
        request.state.request_identifier = request_identifier
        context_token = set_request_identifier(request_identifier)

        started_at = time.perf_counter()
        request_path = request.url.path
        client_address = request.client.host if request.client is not None else "unknown"

        try:
            if not _is_rate_limit_exempt(request_path):
                allowed, retry_after = self._rate_limiter.check_and_consume(
                    client_address=client_address, scope="general"
                )
                if not allowed:
                    response: Response = JSONResponse(
                        status_code=429,
                        headers={"Retry-After": str(retry_after)},
                        content={
                            "code": "rate_limit_exceeded",
                            "message": "Слишком много запросов. Пожалуйста, повторите попытку чуть позже.",
                            "details": {"retry_after_seconds": retry_after},
                            "request_id": request_identifier,
                        },
                    )
                    _apply_response_headers(response, request_path=request_path, request_identifier=request_identifier)
                    duration_ms = (time.perf_counter() - started_at) * 1000.0
                    self._metrics_collector.record_request(status_code=429, duration_milliseconds=duration_ms)
                    return response

            response = await call_next(request)
            _apply_response_headers(response, request_path=request_path, request_identifier=request_identifier)
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            self._metrics_collector.record_request(status_code=response.status_code, duration_milliseconds=duration_ms)

            if not request_path.startswith("/static/"):
                logger.info(
                    "HTTP %s %s -> %s (%.1f мс)", request.method, request_path, response.status_code, duration_ms
                )
            return response
        finally:
            reset_request_identifier(context_token)


def _is_rate_limit_exempt(request_path: str) -> bool:
    return request_path.startswith("/static/") or request_path in {"/health", "/metrics"}


def _apply_response_headers(response: Response, *, request_path: str, request_identifier: str) -> None:
    response.headers["X-Request-ID"] = request_identifier
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request_path == "/widget":
        response.headers["Content-Security-Policy"] = "frame-ancestors *"
    else:
        response.headers.setdefault("X-Frame-Options", "DENY")

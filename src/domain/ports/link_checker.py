"""Контрактный порт проверки актуальности и доступности ссылки источника."""

from typing import Protocol, runtime_checkable

from src.domain.entities import LinkCheckResult

__all__ = ["LinkCheckerPort"]


@runtime_checkable
class LinkCheckerPort(Protocol):
    """Протокол проверки доступности URL источника при верификации карточек."""

    async def check_link(self, *, target_url: str, timeout_seconds: float = 4.0) -> LinkCheckResult:
        """Проверяет, отвечает ли ссылка на первоисточник успешным HTTP-статусом.

        Сетевые сбои, тайм-ауты и ошибки 4xx/5xx возвращаются как `is_reachable=False`,
        чтобы карточка направлялась в очередь модерации без падения конвейера.

        Arguments:
            target_url: Проверяемый адрес первоисточника.
            timeout_seconds: Максимальное время ожидания ответа в секундах.

        Returns:
            Структурированный результат проверки доступности ссылки.
        """
        ...

"""Контрактные порты запуска ИИ-агента и передачи прикладных обработчиков инструментов."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from src.core.types import EventCategory
from src.domain.entities import (
    AgentTurnResult,
    DialogSessionState,
    Event,
    EventSearchFilter,
    EventSearchPage,
    PendingAction,
    WeekendPlanRequest,
    WeekendPlanResult,
)

__all__ = ["AgentOrchestratorPort", "AgentToolCallbacks"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentToolCallbacks:
    """Контейнер прикладных обработчиков инструментов, передаваемых ИИ-агенту.

    Позволяет передавать в адаптер языковой модели готовые функции поиска, планирования
    и отложенных подтверждений напрямую без промежуточного зеркального прокси-класса.

    Attributes:
        search_events: Обработчик поиска мероприятий по `EventSearchFilter`.
        get_event: Обработчик получения полной карточки мероприятия по идентификатору.
        find_similar: Обработчик подбора похожих мероприятий.
        get_venue_schedule: Обработчик чтения расписания площадки.
        plan_weekend: Обработчик детерминированного планировщика «Выходной день».
        prepare_subscribe: Обработчик создания отложенного подтверждения подписки.
        prepare_feedback: Обработчик создания отложенного подтверждения заявки организатору.
    """

    search_events: Callable[[EventSearchFilter], Awaitable[EventSearchPage]]
    get_event: Callable[[UUID], Awaitable[Event]]
    find_similar: Callable[
        [UUID, str | None, datetime | None, datetime | None, bool, int], Awaitable[tuple[Event, ...]]
    ]
    get_venue_schedule: Callable[[str, datetime | None, datetime | None, int], Awaitable[tuple[Event, ...]]]
    plan_weekend: Callable[[WeekendPlanRequest], Awaitable[WeekendPlanResult]]
    prepare_subscribe: Callable[[str | None, EventCategory | None], Awaitable[PendingAction]]
    prepare_feedback: Callable[[UUID | None, str, str | None], Awaitable[PendingAction]]


@runtime_checkable
class AgentOrchestratorPort(Protocol):
    """Протокол выполнения одного диалогового хода ИИ-агентом поверх языковой модели."""

    async def run_turn(
        self,
        *,
        session_state: DialogSessionState,
        user_message: str,
        current_datetime: datetime,
        tool_callbacks: AgentToolCallbacks,
        maximum_steps: int = 5,
    ) -> AgentTurnResult:
        """Выполняет шаг диалога с агентом в рамках контракта инструментов и лимита шагов.

        Arguments:
            session_state: Состояние диалога с историей не менее пяти реплик.
            user_message: Текущий текст сообщения пользователя.
            current_datetime: Опорные дата и время для интерпретации «сегодня/завтра/выходные».
            tool_callbacks: Набор прикладных обработчиков инструментов текущего запроса.
            maximum_steps: Максимальное число вызовов инструментов за один ответ (по умолчанию 5).

        Returns:
            Результат шага агента с карточками, пояснениями, трассировкой и стоимостью.

        Raises:
            AccessException: При невалидных учётных данных или адресе провайдера модели.
            ModelException: При сбое генерации, блокировке или нарушении схемы вывода.
            NetworkException: При сетевом сбое или превышении тайм-аута вызова модели.
            PromptInjectionDetectedError: При перехвате попытки инъекции защитным фильтром.
            ServiceAvailabilityException: При перегрузке или временной недоступности провайдера.
        """
        ...

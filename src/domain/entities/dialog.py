"""Неизменяемые доменные сущности диалога, ИИ-агента, трассировки и контроля бюджета."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from src.core.types import DialogChannel
from src.domain.entities._helpers import (
    EMPTY_OBJECT_MAPPING,
    freeze_mapping,
    restore_dataclass_state,
    serialize_dataclass_state,
)
from src.domain.entities.events import Event

__all__ = [
    "AgentToolInvocation",
    "AgentTraceRecord",
    "AgentTurnResult",
    "DialogMessage",
    "DialogRole",
    "DialogSessionState",
    "PendingAction",
    "PendingActionType",
    "UsageBudgetStatus",
]

type DialogRole = Literal["user", "assistant"]
type PendingActionType = Literal["subscribe", "save_feedback"]

_EMPTY_EXPLANATIONS: Final[Mapping[UUID, str]] = MappingProxyType({})


@dataclass(frozen=True, slots=True, kw_only=True)
class DialogMessage:
    """Отдельная реплика в истории диалога пользователя с сервисом.

    Attributes:
        role: Автор реплики (`user` или `assistant`).
        content: Текст реплики без персональных данных.
        created_at: Временная метка сообщения.
    """

    role: DialogRole
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingAction:
    """Отложенное действие, изменяющее данные и ожидающее подтверждения пользователя.

    Attributes:
        action_identifier: Уникальный идентификатор ожидающего подтверждения действия.
        session_identifier: Идентификатор диалоговой сессии.
        channel: Канал взаимодействия пользователя.
        action_type: Тип изменяющего действия (`subscribe` или `save_feedback`).
        payload: Неизменяемые параметры действия для выполнения после подтверждения.
        confirmation_prompt: Вопрос пользователю с кратким описанием параметров действия.
        created_at: Временная метка создания запроса на подтверждение.
        expires_at: Время истечения срока действия подтверждения.
    """

    action_identifier: UUID
    session_identifier: str
    channel: DialogChannel
    action_type: PendingActionType
    payload: Mapping[str, object] = field(default=EMPTY_OBJECT_MAPPING, hash=False)
    confirmation_prompt: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        """Обеспечивает глубокую неизменяемость параметров отложенного действия."""
        object.__setattr__(self, "payload", freeze_mapping(self.payload))

    def __getstate__(self) -> dict[str, object]:
        """Подготавливает состояние объекта для сериализации."""
        return serialize_dataclass_state(self)

    def __setstate__(self, state: Mapping[str, object]) -> None:
        """Восстанавливает неизменяемое состояние объекта после десериализации."""
        restore_dataclass_state(self, state, self.__post_init__)


@dataclass(frozen=True, slots=True, kw_only=True)
class DialogSessionState:
    """Текущее состояние многошаговой диалоговой сессии (память не менее пяти реплик).

    Attributes:
        session_identifier: Стабильный идентификатор диалога.
        channel: Канал диалога (`web`, `telegram`, `evaluation`).
        subscriber_identifier: Технический идентификатор пользователя для привязки подписки.
        recent_messages: Последние реплики диалога в хронологическом порядке.
        active_pending_action: Активное действие, ожидающее подтверждения, если оно есть.
    """

    session_identifier: str
    channel: DialogChannel
    subscriber_identifier: str
    recent_messages: tuple[DialogMessage, ...] = ()
    active_pending_action: PendingAction | None = None

    def __post_init__(self) -> None:
        """Фиксирует кортеж последних реплик диалога."""
        object.__setattr__(self, "recent_messages", tuple(self.recent_messages))


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentToolInvocation:
    """Запись об одном вызове инструмента ИИ-агентом в ходе ответа.

    Attributes:
        tool_name: Имя вызванного инструмента из контракта.
        arguments: Неизменяемый словарь аргументов, переданных инструменту.
        returned_event_identifiers: Идентификаторы карточек событий, возвращённых инструментом.
        succeeded: Признак успешного выполнения инструмента без ошибки.
        error_message: Сообщение об ошибке инструмента, если она возникла.
    """

    tool_name: str
    arguments: Mapping[str, object] = field(default=EMPTY_OBJECT_MAPPING, hash=False)
    returned_event_identifiers: tuple[UUID, ...] = ()
    succeeded: bool = True
    error_message: str | None = None

    def __post_init__(self) -> None:
        """Обеспечивает неизменяемость словаря аргументов и списка идентификаторов."""
        object.__setattr__(self, "arguments", freeze_mapping(self.arguments))
        object.__setattr__(self, "returned_event_identifiers", tuple(self.returned_event_identifiers))

    def __getstate__(self) -> dict[str, object]:
        """Подготавливает состояние объекта для сериализации."""
        return serialize_dataclass_state(self)

    def __setstate__(self, state: Mapping[str, object]) -> None:
        """Восстанавливает неизменяемое состояние объекта после десериализации."""
        restore_dataclass_state(self, state, self.__post_init__)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentTurnResult:
    """Итоговый результат обработки пользовательского сообщения агентом или фоллбэком.

    Attributes:
        answer_text: Финальный текст ответа пользователю.
        recommended_events: Подтверждённые карточки событий (не более 3–5 по контракту).
        explanations_by_event: Пояснения в 1–2 предложения по каждому рекомендованному событию.
        clarifying_question: Один уточняющий вопрос при неоднозначности запроса.
        pending_action: Созданное отложенное действие, требующее подтверждения.
        fallback_used: Признак срабатывания резервного детерминированного режима.
        fallback_reason: Причина перехода в резервный режим.
        trace_identifier: Идентификатор сохранённой трассы ответа.
        tool_invocations: Последовательность вызовов инструментов за данный шаг.
        tokens_input: Количество входных токенов языковой модели.
        tokens_output: Количество выходных токенов языковой модели.
        cost_rubles: Расчётная стоимость ответа в рублях.
        latency_milliseconds: Полная длительность обработки запроса в миллисекундах.
        model_name: Фактическое имя модели (или `"deterministic-fallback"`).
        prompt_version: Версия системного промпта для воспроизводимости метрик.
    """

    answer_text: str
    recommended_events: tuple[Event, ...] = ()
    explanations_by_event: Mapping[UUID, str] = field(default=_EMPTY_EXPLANATIONS, hash=False)
    clarifying_question: str | None = None
    pending_action: PendingAction | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    trace_identifier: UUID | None = None
    tool_invocations: tuple[AgentToolInvocation, ...] = ()
    tokens_input: int = 0
    tokens_output: int = 0
    cost_rubles: float = 0.0
    latency_milliseconds: int = 0
    model_name: str = ""
    prompt_version: str = ""

    def __post_init__(self) -> None:
        """Обеспечивает глубокую неизменяемость коллекций и пояснений результата."""
        object.__setattr__(self, "recommended_events", tuple(self.recommended_events))
        object.__setattr__(self, "explanations_by_event", freeze_mapping(self.explanations_by_event))
        object.__setattr__(self, "tool_invocations", tuple(self.tool_invocations))

    def __getstate__(self) -> dict[str, object]:
        """Подготавливает состояние объекта для сериализации."""
        return serialize_dataclass_state(self)

    def __setstate__(self, state: Mapping[str, object]) -> None:
        """Восстанавливает неизменяемое состояние объекта после десериализации."""
        restore_dataclass_state(self, state, self.__post_init__)


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentTraceRecord:
    """Полная трасса ответа ИИ-агента, доступная из админки и по прямой ссылке.

    Attributes:
        trace_identifier: Уникальный идентификатор трассы.
        request_identifier: Сквозной идентификатор HTTP/Telegram-запроса.
        session_identifier: Идентификатор диалоговой сессии.
        channel: Канал поступления запроса.
        user_query: Исходный текст запроса пользователя.
        plan_summary: Краткое описание плана выполнения запроса.
        tool_invocations: Список вызовов инструментов с аргументами и идентификаторами.
        grounded_event_identifiers: Идентификаторы записей базы, ставших основанием ответа.
        fallback_used: Признак работы в резервном режиме без языковой модели.
        latency_milliseconds: Время ответа в миллисекундах.
        tokens_input: Число входных токенов.
        tokens_output: Число выходных токенов.
        cost_rubles: Оценка стоимости запроса в рублях.
        model_name: Использованная языковая модель.
        prompt_version: Идентификатор версии системного промпта.
        created_at: Временная метка сохранения трассы.
    """

    trace_identifier: UUID
    request_identifier: str
    session_identifier: str
    channel: DialogChannel
    user_query: str
    plan_summary: str
    tool_invocations: tuple[AgentToolInvocation, ...] = ()
    grounded_event_identifiers: tuple[UUID, ...] = ()
    fallback_used: bool = False
    latency_milliseconds: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost_rubles: float = 0.0
    model_name: str = ""
    prompt_version: str = ""
    created_at: datetime

    def __post_init__(self) -> None:
        """Фиксирует кортежи вызовов инструментов и подтверждённых идентификаторов."""
        object.__setattr__(self, "tool_invocations", tuple(self.tool_invocations))
        object.__setattr__(self, "grounded_event_identifiers", tuple(self.grounded_event_identifiers))


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageBudgetStatus:
    """Текущее состояние суточных финансовых лимитов использования языковой модели.

    Attributes:
        user_daily_spent_rubles: Сумма расходов данного пользователя за текущие сутки в рублях.
        user_daily_limit_rubles: Суточный лимит на одного пользователя в рублях.
        global_daily_spent_rubles: Общие расходы сервиса за текущие сутки в рублях.
        global_daily_limit_rubles: Общий суточный лимит сервиса в рублях.
        is_budget_exceeded: Признак превышения любого из установленных лимитов.
    """

    user_daily_spent_rubles: float
    user_daily_limit_rubles: float
    global_daily_spent_rubles: float
    global_daily_limit_rubles: float
    is_budget_exceeded: bool

"""Доменные исключения и реестр стандартных сообщений об ошибках бизнес-логики."""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from src.core.exceptions import DomainException
from src.core.types import DialogChannel, RealCollection

__all__ = [
    "DEFAULT_DOMAIN_ERROR_MESSAGES",
    "BudgetExceededError",
    "BudgetScope",
    "ConfirmationRequiredError",
    "DomainErrorKey",
    "DuplicateEventConflictError",
    "EventNotFoundError",
    "HallucinatedEventError",
    "InvalidEventDataError",
    "InvalidSearchFilterError",
    "InvalidWeekendPlanRequestError",
    "ModerationItemNotFoundError",
    "PendingActionExpiredError",
    "PendingActionNotFoundError",
    "PromptInjectionDetectedError",
    "RateLimitExceededError",
    "SubscriptionNotFoundError",
    "UnsupportedSourceFormatError",
]

type BudgetScope = Literal["dialog", "user_daily", "global_daily"]


class DomainErrorKey(StrEnum):
    """Ключи стандартных сообщений доменных ошибок.

    Attributes:
        EVENT_NOT_FOUND: Мероприятие не найдено в каталоге.
        INVALID_EVENT_DATA: Нарушены правила валидации карточки мероприятия.
        DUPLICATE_EVENT_CONFLICT: Конфликт дедупликации при сохранении мероприятия.
        MODERATION_ITEM_NOT_FOUND: Запись очереди модерации не найдена.
        SUBSCRIPTION_NOT_FOUND: Подписка пользователя не найдена.
        BUDGET_EXCEEDED: Исчерпан лимит стоимости использования языковой модели.
        RATE_LIMIT_EXCEEDED: Превышена допустимая частота запросов пользователя.
        CONFIRMATION_REQUIRED: Изменяющее данные действие требует подтверждения пользователя.
        PENDING_ACTION_NOT_FOUND: Ожидающее подтверждения действие не найдено.
        PENDING_ACTION_EXPIRED: Срок действия подтверждения истёк.
        PROMPT_INJECTION_DETECTED: Обнаружена попытка подмены инструкций в данных или запросе.
        HALLUCINATED_EVENT: В ответе модели обнаружен выдуманный идентификатор события.
        UNSUPPORTED_SOURCE_FORMAT: Неподдерживаемый формат файла или внешнего источника.
        INVALID_SEARCH_FILTER: Некорректные параметры фильтрации поиска.
        INVALID_WEEKEND_PLAN_REQUEST: Некорректные параметры запроса планировщика «Выходной день».
    """

    EVENT_NOT_FOUND = "event_not_found"
    INVALID_EVENT_DATA = "invalid_event_data"
    DUPLICATE_EVENT_CONFLICT = "duplicate_event_conflict"
    MODERATION_ITEM_NOT_FOUND = "moderation_item_not_found"
    SUBSCRIPTION_NOT_FOUND = "subscription_not_found"
    BUDGET_EXCEEDED = "budget_exceeded"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    CONFIRMATION_REQUIRED = "confirmation_required"
    PENDING_ACTION_NOT_FOUND = "pending_action_not_found"
    PENDING_ACTION_EXPIRED = "pending_action_expired"
    PROMPT_INJECTION_DETECTED = "prompt_injection_detected"
    HALLUCINATED_EVENT = "hallucinated_event"
    UNSUPPORTED_SOURCE_FORMAT = "unsupported_source_format"
    INVALID_SEARCH_FILTER = "invalid_search_filter"
    INVALID_WEEKEND_PLAN_REQUEST = "invalid_weekend_plan_request"


DEFAULT_DOMAIN_ERROR_MESSAGES: Final[Mapping[DomainErrorKey, str]] = MappingProxyType(
    {
        DomainErrorKey.EVENT_NOT_FOUND: "Запрошенное мероприятие не найдено.",
        DomainErrorKey.INVALID_EVENT_DATA: "Данные мероприятия не соответствуют правилам каталога.",
        DomainErrorKey.DUPLICATE_EVENT_CONFLICT: "Обнаружен конфликт дублирующейся карточки мероприятия.",
        DomainErrorKey.MODERATION_ITEM_NOT_FOUND: "Элемент очереди модерации не найден.",
        DomainErrorKey.SUBSCRIPTION_NOT_FOUND: "Подписка не найдена.",
        DomainErrorKey.BUDGET_EXCEEDED: "Превышен лимит стоимости работы ИИ-помощника.",
        DomainErrorKey.RATE_LIMIT_EXCEEDED: "Слишком много запросов за короткое время. Попробуйте чуть позже.",
        DomainErrorKey.CONFIRMATION_REQUIRED: "Действие, изменяющее данные, требует явного подтверждения.",
        DomainErrorKey.PENDING_ACTION_NOT_FOUND: "Запрос на подтверждение действия не найден.",
        DomainErrorKey.PENDING_ACTION_EXPIRED: "Срок действия запроса на подтверждение истёк.",
        DomainErrorKey.PROMPT_INJECTION_DETECTED: "Обнаружена попытка вмешательства в инструкции ИИ-помощника.",
        DomainErrorKey.HALLUCINATED_EVENT: "Ответ содержит ссылку на мероприятие вне результатов инструментов.",
        DomainErrorKey.UNSUPPORTED_SOURCE_FORMAT: "Формат входного файла или внешнего источника не поддерживается.",
        DomainErrorKey.INVALID_SEARCH_FILTER: "Некорректные параметры фильтра поиска мероприятий.",
        DomainErrorKey.INVALID_WEEKEND_PLAN_REQUEST: "Некорректные параметры для планирования выходного дня.",
    }
)


def _resolve_error_message(error_key: DomainErrorKey, message: str | None) -> str:
    """Возвращает переданное сообщение или стандартную формулировку из реестра.

    Arguments:
        error_key: Ключ стандартного сообщения ошибки.
        message: Пользовательское сообщение об ошибке, если задано.

    Returns:
        Итоговая непустая строка описания ошибки.
    """
    if message is not None and message.strip():
        return message
    return DEFAULT_DOMAIN_ERROR_MESSAGES[error_key]


def _freeze_collection[ItemType](items: RealCollection[ItemType] | None) -> tuple[ItemType, ...]:
    """Преобразует конечную коллекцию в неизменяемый кортеж.

    Arguments:
        items: Исходная коллекция элементов или `None`.

    Returns:
        Неизменяемый кортеж элементов.
    """
    if items is None:
        return ()
    return tuple(items)


class EventNotFoundError(DomainException):
    """Ошибка отсутствия карточки мероприятия в каталоге.

    Attributes:
        event_identifier: Идентификатор запрошенного мероприятия.
        reason: Дополнительная причина отсутствия.
    """

    def __init__(
        self, message: str | None = None, *, event_identifier: UUID | None = None, reason: str | None = None
    ) -> None:
        """Инициализирует ошибку отсутствующего мероприятия.

        Arguments:
            message: Человекочитаемое описание ошибки.
            event_identifier: Идентификатор ненайденного мероприятия.
            reason: Уточнение причины ошибки.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.EVENT_NOT_FOUND, message))
        self.event_identifier: UUID | None = event_identifier
        self.reason: str | None = reason


class InvalidEventDataError(DomainException):
    """Ошибка нарушения доменных правил заполнения карточки мероприятия.

    Attributes:
        field_name: Имя поля, не прошедшего доменную проверку.
        reason: Конкретная причина отклонения значения.
    """

    def __init__(self, message: str | None = None, *, field_name: str | None = None, reason: str | None = None) -> None:
        """Инициализирует ошибку некорректных данных мероприятия.

        Arguments:
            message: Человекочитаемое описание ошибки.
            field_name: Имя проблемного поля карточки.
            reason: Объяснение нарушения правила.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.INVALID_EVENT_DATA, message))
        self.field_name: str | None = field_name
        self.reason: str | None = reason


class DuplicateEventConflictError(DomainException):
    """Ошибка конфликта при сохранении дублирующегося мероприятия.

    Attributes:
        deduplication_key: Вычисленный ключ дедупликации.
        existing_event_identifier: Идентификатор уже существующей карточки.
        reason: Описание причины конфликта.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        deduplication_key: str | None = None,
        existing_event_identifier: UUID | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку конфликта дедупликации.

        Arguments:
            message: Человекочитаемое описание ошибки.
            deduplication_key: Ключ дедупликации конфликтующих карточек.
            existing_event_identifier: Идентификатор существующей записи в базе.
            reason: Причина невозможности объединения карточек.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.DUPLICATE_EVENT_CONFLICT, message))
        self.deduplication_key: str | None = deduplication_key
        self.existing_event_identifier: UUID | None = existing_event_identifier
        self.reason: str | None = reason


class ModerationItemNotFoundError(DomainException):
    """Ошибка отсутствия элемента в очереди модерации.

    Attributes:
        moderation_identifier: Идентификатор запрошенной задачи модерации.
        reason: Уточнение причины ошибки.
    """

    def __init__(
        self, message: str | None = None, *, moderation_identifier: UUID | None = None, reason: str | None = None
    ) -> None:
        """Инициализирует ошибку отсутствующей задачи модерации.

        Arguments:
            message: Человекочитаемое описание ошибки.
            moderation_identifier: Идентификатор элемента модерации.
            reason: Дополнительный контекст ошибки.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.MODERATION_ITEM_NOT_FOUND, message))
        self.moderation_identifier: UUID | None = moderation_identifier
        self.reason: str | None = reason


class SubscriptionNotFoundError(DomainException):
    """Ошибка отсутствия подписки при просмотре или удалении.

    Attributes:
        subscription_identifier: Идентификатор подписки.
        subscriber_identifier: Технический идентификатор пользователя.
        reason: Уточнение причины отсутствия.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        subscription_identifier: UUID | None = None,
        subscriber_identifier: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку отсутствующей подписки.

        Arguments:
            message: Человекочитаемое описание ошибки.
            subscription_identifier: Идентификатор записи подписки.
            subscriber_identifier: Технический идентификатор подписчика.
            reason: Дополнительное описание причины.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.SUBSCRIPTION_NOT_FOUND, message))
        self.subscription_identifier: UUID | None = subscription_identifier
        self.subscriber_identifier: str | None = subscriber_identifier
        self.reason: str | None = reason


class BudgetExceededError(DomainException):
    """Ошибка исчерпания финансового лимита вызовов языковой модели.

    Служит сигналом для автоматического переключения оркестратора в резервный режим.

    Attributes:
        scope: Уровень сработавшего лимита (`dialog`, `user_daily`, `global_daily`).
        spent_rubles: Фактически израсходованная сумма в рублях.
        limit_rubles: Установленный порог расходов в рублях.
        reason: Дополнительное пояснение причины ограничения.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        scope: BudgetScope | None = None,
        spent_rubles: float | None = None,
        limit_rubles: float | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку превышения лимита стоимости.

        Arguments:
            message: Человекочитаемое описание ошибки.
            scope: Область действия превышенного лимита.
            spent_rubles: Текущие расходы в рублях.
            limit_rubles: Максимально допустимые расходы в рублях.
            reason: Дополнительная причина срабатывания лимита.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.BUDGET_EXCEEDED, message))
        self.scope: BudgetScope | None = scope
        self.spent_rubles: float | None = spent_rubles
        self.limit_rubles: float | None = limit_rubles
        self.reason: str | None = reason


class RateLimitExceededError(DomainException):
    """Ошибка превышения допустимой частоты запросов в канале взаимодействия.

    Attributes:
        channel: Канал, в котором сработало ограничение частоты.
        retry_after_seconds: Рекомендуемое время ожидания до повтора в секундах.
        reason: Причина срабатывания ограничения.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        channel: DialogChannel | None = None,
        retry_after_seconds: int | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку превышения частоты запросов.

        Arguments:
            message: Человекочитаемое описание ошибки.
            channel: Канал поступления запроса.
            retry_after_seconds: Пауза до следующего запроса в секундах.
            reason: Дополнительный контекст срабатывания защиты.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.RATE_LIMIT_EXCEEDED, message))
        self.channel: DialogChannel | None = channel
        self.retry_after_seconds: int | None = retry_after_seconds
        self.reason: str | None = reason


class ConfirmationRequiredError(DomainException):
    """Ошибка попытки выполнить изменяющее действие без подтверждения пользователя.

    Attributes:
        action_type: Тип действия, требующего подтверждения.
        action_identifier: Идентификатор созданного ожидающего действия.
        reason: Пояснение политики безопасности.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        action_type: str | None = None,
        action_identifier: UUID | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку необходимости подтверждения.

        Arguments:
            message: Человекочитаемое описание ошибки.
            action_type: Тип изменяющей операции (`subscribe`, `save_feedback`).
            action_identifier: Идентификатор ожидающего действия.
            reason: Причина требования подтверждения.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.CONFIRMATION_REQUIRED, message))
        self.action_type: str | None = action_type
        self.action_identifier: UUID | None = action_identifier
        self.reason: str | None = reason


class PendingActionNotFoundError(DomainException):
    """Ошибка отсутствия ожидающего подтверждения действия.

    Attributes:
        action_identifier: Идентификатор ожидающего действия.
        session_identifier: Идентификатор диалоговой сессии.
        reason: Дополнительная причина ошибки.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        action_identifier: UUID | None = None,
        session_identifier: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку отсутствующего запроса на подтверждение.

        Arguments:
            message: Человекочитаемое описание ошибки.
            action_identifier: Идентификатор отложенного действия.
            session_identifier: Идентификатор диалоговой сессии.
            reason: Пояснение причины отсутствия.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.PENDING_ACTION_NOT_FOUND, message))
        self.action_identifier: UUID | None = action_identifier
        self.session_identifier: str | None = session_identifier
        self.reason: str | None = reason


class PendingActionExpiredError(DomainException):
    """Ошибка истечения срока жизни запроса на подтверждение действия.

    Attributes:
        action_identifier: Идентификатор просроченного действия.
        expired_at: Момент окончания срока действия подтверждения.
        reason: Дополнительный контекст ошибки.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        action_identifier: UUID | None = None,
        expired_at: datetime | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку просроченного подтверждения.

        Arguments:
            message: Человекочитаемое описание ошибки.
            action_identifier: Идентификатор отложенного действия.
            expired_at: Временная метка истечения срока.
            reason: Дополнительное описание.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.PENDING_ACTION_EXPIRED, message))
        self.action_identifier: UUID | None = action_identifier
        self.expired_at: datetime | None = expired_at
        self.reason: str | None = reason


class PromptInjectionDetectedError(DomainException):
    """Ошибка выявления попытки инъекции инструкций в тексте пользователя или источника.

    Attributes:
        source_context: Источник подозрительного текста (`user_message`, `external_event` и т.п.).
        matched_patterns: Неизменяемый кортеж сработавших сигнатур защиты.
        reason: Описание зафиксированной угрозы.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        source_context: str | None = None,
        matched_patterns: RealCollection[str] | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку обнаружения инъекции инструкций.

        Arguments:
            message: Человекочитаемое описание ошибки.
            source_context: Контекст, в котором обнаружена инъекция.
            matched_patterns: Сработавшие правила или паттерны обнаружения.
            reason: Дополнительное пояснение инцидента.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.PROMPT_INJECTION_DETECTED, message))
        self.source_context: str | None = source_context
        self.matched_patterns: tuple[str, ...] = _freeze_collection(matched_patterns)
        self.reason: str | None = reason


class HallucinatedEventError(DomainException):
    """Ошибка обнаружения выдуманного идентификатора события в ответе модели.

    Attributes:
        ungrounded_event_identifiers: Кортеж идентификаторов, отсутствующих в результатах инструментов.
        reason: Описание нарушения контракта обоснованности.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        ungrounded_event_identifiers: RealCollection[UUID] | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку нарушения контракта обоснованности фактов.

        Arguments:
            message: Человекочитаемое описание ошибки.
            ungrounded_event_identifiers: Идентификаторы событий вне результатов инструментов.
            reason: Дополнительное пояснение сбоя проверки.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.HALLUCINATED_EVENT, message))
        self.ungrounded_event_identifiers: tuple[UUID, ...] = _freeze_collection(ungrounded_event_identifiers)
        self.reason: str | None = reason


class UnsupportedSourceFormatError(DomainException):
    """Ошибка неподдерживаемого формата файла импорта или внешнего источника.

    Attributes:
        format_name: Обнаруженный или запрошенный формат данных.
        source_name: Имя источника или загружаемого файла.
        reason: Причина невозможности разбора.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        format_name: str | None = None,
        source_name: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Инициализирует ошибку неподдерживаемого формата источника.

        Arguments:
            message: Человекочитаемое описание ошибки.
            format_name: Название проблемного формата.
            source_name: Название источника или файла.
            reason: Описание причины отказа.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.UNSUPPORTED_SOURCE_FORMAT, message))
        self.format_name: str | None = format_name
        self.source_name: str | None = source_name
        self.reason: str | None = reason


class InvalidSearchFilterError(DomainException):
    """Ошибка некорректного сочетания или значения параметров поиска.

    Attributes:
        parameter_name: Имя некорректного параметра фильтрации.
        reason: Пояснение доменного ограничения.
    """

    def __init__(
        self, message: str | None = None, *, parameter_name: str | None = None, reason: str | None = None
    ) -> None:
        """Инициализирует ошибку параметров поискового фильтра.

        Arguments:
            message: Человекочитаемое описание ошибки.
            parameter_name: Имя некорректного поля фильтра.
            reason: Причина отклонения значения.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.INVALID_SEARCH_FILTER, message))
        self.parameter_name: str | None = parameter_name
        self.reason: str | None = reason


class InvalidWeekendPlanRequestError(DomainException):
    """Ошибка некорректного запроса к планировщику «Выходной день».

    Attributes:
        parameter_name: Имя некорректного параметра запроса планировщика.
        reason: Пояснение причины невалидности.
    """

    def __init__(
        self, message: str | None = None, *, parameter_name: str | None = None, reason: str | None = None
    ) -> None:
        """Инициализирует ошибку параметров запроса планировщика.

        Arguments:
            message: Человекочитаемое описание ошибки.
            parameter_name: Имя некорректного параметра.
            reason: Объяснение ошибки валидации.
        """
        super().__init__(_resolve_error_message(DomainErrorKey.INVALID_WEEKEND_PLAN_REQUEST, message))
        self.parameter_name: str | None = parameter_name
        self.reason: str | None = reason

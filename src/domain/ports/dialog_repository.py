"""Контрактный порт памяти диалогов, отложенных подтверждений, трасс и бюджета модели."""

from datetime import date, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from src.core.types import DialogChannel, RealCollection
from src.domain.entities import AgentTraceRecord, DialogMessage, DialogSessionState, PendingAction, UsageBudgetStatus

__all__ = ["DialogRepositoryPort"]


@runtime_checkable
class DialogRepositoryPort(Protocol):
    """Протокол персистентного состояния диалоговых сессий и учёта расходов на ИИ."""

    async def get_session_state(
        self, *, session_identifier: str, channel: DialogChannel, subscriber_identifier: str, message_limit: int = 10
    ) -> DialogSessionState:
        """Загружает состояние сессии диалога с последними репликами и ожидающим действием.

        Если сессия ещё не существует, создаёт её и возвращает пустую историю сообщений.

        Arguments:
            session_identifier: Стабильный идентификатор диалоговой сессии.
            channel: Канал взаимодействия пользователя.
            subscriber_identifier: Технический идентификатор пользователя без персональных данных.
            message_limit: Максимальное количество последних реплик (не менее 5 по контракту).

        Returns:
            Текущее неизменяемое состояние диалоговой сессии.

        Raises:
            InfrastructureException: При ошибке чтения или создания сессии в хранилище.
        """
        ...

    async def append_messages(self, *, session_identifier: str, messages: RealCollection[DialogMessage]) -> None:
        """Добавляет новые реплики пользователя и ассистента в историю сессии.

        Arguments:
            session_identifier: Идентификатор диалоговой сессии.
            messages: Конечная коллекция добавляемых сообщений.

        Raises:
            InfrastructureException: При ошибке записи реплик в хранилище.
        """
        ...

    async def create_pending_action(self, *, pending_action: PendingAction) -> PendingAction:
        """Регистрирует отложенное изменяющее действие, ожидающее подтверждения.

        Деактивирует предыдущее неподтверждённое действие той же сессии, если оно было.

        Arguments:
            pending_action: Создаваемое отложенное действие.

        Returns:
            Сохранённое отложенное действие.

        Raises:
            InfrastructureException: При ошибке записи действия в хранилище.
        """
        ...

    async def consume_pending_action(
        self, *, action_identifier: UUID, session_identifier: str, current_time: datetime
    ) -> PendingAction:
        """Атомарно проверяет и закрывает отложенное действие после подтверждения пользователем.

        Arguments:
            action_identifier: Идентификатор подтверждаемого действия.
            session_identifier: Идентификатор сессии, приславшей подтверждение.
            current_time: Текущее время проверки срока действия.

        Returns:
            Подтверждённое отложенное действие с параметрами исполнения.

        Raises:
            InfrastructureException: При ошибке обновления состояния в хранилище.
            PendingActionExpiredError: Если срок действия подтверждения истёк.
            PendingActionNotFoundError: Если действие отсутствует или принадлежит другой сессии.
        """
        ...

    async def cancel_pending_action(self, *, action_identifier: UUID, session_identifier: str) -> None:
        """Отменяет отложенное действие по инициативе пользователя.

        Arguments:
            action_identifier: Идентификатор отменяемого действия.
            session_identifier: Идентификатор сессии владельца.

        Raises:
            InfrastructureException: При ошибке удаления или отмены в хранилище.
            PendingActionNotFoundError: Если активное действие не найдено.
        """
        ...

    async def save_trace(self, *, trace: AgentTraceRecord) -> AgentTraceRecord:
        """Сохраняет полную трассу ответа ИИ-агента или резервного поиска.

        Arguments:
            trace: Неизменяемая запись трассировки ответа.

        Returns:
            Сохранённая запись трассы.

        Raises:
            InfrastructureException: При ошибке сохранения трассы в хранилище.
        """
        ...

    async def get_trace(self, *, trace_identifier: UUID) -> AgentTraceRecord | None:
        """Возвращает сохранённую трассу ответа по её идентификатору.

        Arguments:
            trace_identifier: Идентификатор трассы.

        Returns:
            Запись трассы или `None`, если она не найдена.

        Raises:
            InfrastructureException: При ошибке чтения из хранилища.
        """
        ...

    async def list_recent_traces(self, *, limit: int = 50) -> tuple[AgentTraceRecord, ...]:
        """Возвращает последние трассы ответов для просмотра в интерфейсе администрирования.

        Arguments:
            limit: Максимальное количество последних трасс.

        Returns:
            Кортеж записей трассировки, упорядоченных от новых к старым.

        Raises:
            InfrastructureException: При ошибке чтения из хранилища.
        """
        ...

    async def get_usage_budget_status(
        self,
        *,
        subscriber_identifier: str,
        target_date: date,
        user_daily_limit_rubles: float,
        global_daily_limit_rubles: float,
    ) -> UsageBudgetStatus:
        """Проверяет текущее состояние суточных лимитов стоимости модели.

        Arguments:
            subscriber_identifier: Технический идентификатор пользователя.
            target_date: Дата расчётных суток.
            user_daily_limit_rubles: Порог суточных расходов на одного пользователя в рублях.
            global_daily_limit_rubles: Общий суточный порог расходов сервиса в рублях.

        Returns:
            Снимок расходов и признак превышения лимитов.

        Raises:
            InfrastructureException: При ошибке чтения счётчиков из хранилища.
        """
        ...

    async def record_model_usage(
        self,
        *,
        subscriber_identifier: str,
        target_date: date,
        cost_rubles: float,
        tokens_input: int,
        tokens_output: int,
    ) -> None:
        """Фиксирует израсходованные токены и стоимость вызова модели за текущие сутки.

        Arguments:
            subscriber_identifier: Технический идентификатор пользователя.
            target_date: Дата расчётных суток.
            cost_rubles: Стоимость выполненного вызова в рублях.
            tokens_input: Число входных токенов.
            tokens_output: Число выходных токенов.

        Raises:
            InfrastructureException: При ошибке обновления счётчиков расходов.
        """
        ...

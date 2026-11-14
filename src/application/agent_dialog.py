"""Прикладной оркестратор многошагового диалога с ИИ-агентом и резервным поиском.

Архитектурное решение:
    Сервис связывает персистентную память диалогов (`DialogRepositoryPort`),
    защиту от инъекций инструкций (`inspect_untrusted_text`, `contains_system_prompt_leak`),
    двухшаговое подтверждение изменяющих операций (`subscribe` и `save_feedback`),
    контроль суточных финансовых бюджетов в рублях, запуск ИИ-агента (`AgentOrchestratorPort`)
    с семью прикладными инструментами и бесшовный переход на детерминированный
    резервный поиск (`FallbackSearchService`) при отключении модели, исчерпании
    бюджета или ошибках провайдера.
"""

import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from src.application._helpers import attach_default_timezone, resolve_normalized_city
from src.application.fallback_search import FallbackSearchService
from src.application.search_events import EventSearchService
from src.application.subscriptions import SubscriptionService
from src.application.weekend_planner import WeekendPlannerService
from src.core.configuration import LanguageModelSettings
from src.core.exceptions import ApplicationException, ValidationException
from src.core.logger import get_logger
from src.core.types import DialogChannel, EventCategory
from src.domain.entities import (
    AgentTraceRecord,
    AgentTurnResult,
    DialogMessage,
    Event,
    EventSearchFilter,
    EventSearchPage,
    PendingAction,
    WeekendPlanRequest,
    WeekendPlanResult,
)
from src.domain.exceptions import (
    EventNotFoundError,
    HallucinatedEventError,
    PendingActionExpiredError,
    PendingActionNotFoundError,
    PromptInjectionDetectedError,
)
from src.domain.ports import (
    AdministrationRepositoryPort,
    AgentOrchestratorPort,
    AgentToolCallbacks,
    DialogRepositoryPort,
    EventRepositoryPort,
)
from src.domain.services import (
    contains_system_prompt_leak,
    inspect_untrusted_text,
    normalize_optional_text,
    sanitize_control_characters,
)

__all__ = ["AgentDialogService"]

logger = get_logger(__name__)

_MINIMUM_HISTORY_LIMIT: Final[int] = 5
_MAXIMUM_RECOMMENDED_EVENTS: Final[int] = 5
_FALLBACK_MODEL_NAME: Final[str] = "deterministic-fallback"
_CONFIRMATION_MODEL_NAME: Final[str] = "deterministic-confirmation"

_CONFIRMATION_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "да",
        "да подтверждаю",
        "подтверждаю",
        "подтвердить",
        "подтверждаем",
        "согласен",
        "согласна",
        "оформить",
        "оформляй",
        "сохранить",
        "сохраняй",
        "подписаться",
        "подпиши",
        "отправить",
        "отправляй",
        "ок",
        "окей",
        "хорошо",
        "конечно",
        "угу",
        "ага",
        "yes",
        "confirm",
    }
)

_CANCELLATION_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "нет",
        "отмена",
        "отменить",
        "отменяй",
        "не надо",
        "не нужно",
        "отказ",
        "отказываюсь",
        "передумал",
        "передумала",
        "cancel",
        "no",
    }
)

_CATEGORY_DISPLAY_LABELS: Final[dict[EventCategory, str]] = {
    EventCategory.CONCERT: "концерты",
    EventCategory.SPECTACLE: "спектакли",
    EventCategory.EXHIBITION: "выставки",
    EventCategory.FESTIVAL: "фестивали",
    EventCategory.CINEMA: "кино",
    EventCategory.EXCURSION: "экскурсии",
    EventCategory.OTHER: "другое",
}


class AgentDialogService:
    """Прикладной оркестратор диалогового сценария подбора мероприятий и подписок."""

    def __init__(
        self,
        *,
        event_repository: EventRepositoryPort,
        dialog_repository: DialogRepositoryPort,
        administration_repository: AdministrationRepositoryPort,
        search_service: EventSearchService,
        subscription_service: SubscriptionService,
        weekend_planner: WeekendPlannerService,
        fallback_search: FallbackSearchService,
        agent_orchestrator: AgentOrchestratorPort | None = None,
        language_model_settings: LanguageModelSettings | None = None,
        pending_action_ttl_seconds: int = 600,
        dialog_history_limit: int = 10,
    ) -> None:
        """Инициализирует оркестратор диалогового сценария.

        Arguments:
            event_repository: Порт каталога мероприятий и справочников.
            dialog_repository: Порт хранения сессий, подтверждений, трасс и бюджета.
            administration_repository: Порт подписок, обращений и администрирования.
            search_service: Прикладной сервис поиска и чтения карточек мероприятий.
            subscription_service: Прикладной сервис оформления подписок и обращений.
            weekend_planner: Прикладной планировщик маршрута «Выходной день».
            fallback_search: Сервис детерминированного резервного поиска.
            agent_orchestrator: Необязательный порт ИИ-агента поверх языковой модели.
            language_model_settings: Настройки языковой модели и суточных бюджетов.
            pending_action_ttl_seconds: Время жизни отложенного подтверждения в секундах.
            dialog_history_limit: Максимальное число последних реплик, загружаемых из сессии.
        """
        self._event_repository = event_repository
        self._dialog_repository = dialog_repository
        self._administration_repository = administration_repository
        self._search_service = search_service
        self._subscription_service = subscription_service
        self._weekend_planner = weekend_planner
        self._fallback_search = fallback_search
        self._agent_orchestrator = agent_orchestrator
        self._language_model_settings = language_model_settings
        self._pending_action_ttl_seconds = max(60, pending_action_ttl_seconds)
        self._dialog_history_limit = max(_MINIMUM_HISTORY_LIMIT, dialog_history_limit)

    async def handle_message(
        self,
        *,
        session_identifier: str,
        channel: DialogChannel,
        subscriber_identifier: str,
        user_message: str,
        request_identifier: str | None = None,
        current_datetime: datetime | None = None,
    ) -> AgentTurnResult:
        """Обрабатывает одно сообщение пользователя в рамках диалоговой сессии.

        Arguments:
            session_identifier: Стабильный идентификатор диалоговой сессии.
            channel: Канал поступления сообщения (`web`, `telegram`, `evaluation`).
            subscriber_identifier: Технический идентификатор пользователя без ПДн.
            user_message: Входящий текст сообщения пользователя.
            request_identifier: Сквозной идентификатор запроса для трассировки.
            current_datetime: Опорные дата и время для интерпретации относительных дат.

        Returns:
            Итоговый результат хода с текстом, подтверждёнными карточками и `trace_identifier`.
        """
        started_at = time.perf_counter()
        resolved_request_id = normalize_optional_text(request_identifier) or str(uuid4())
        reference_now = attach_default_timezone(current_datetime or datetime.now())
        trace_id = uuid4()

        inspection = inspect_untrusted_text(user_message)
        if inspection.has_suspicious_instructions:
            logger.warning(
                "Обнаружена попытка инъекции инструкций в сообщении пользователя "
                "(request_id=%s, channel=%s, patterns=%s).",
                resolved_request_id,
                channel.value,
                ",".join(inspection.matched_patterns),
            )
            injection_result = AgentTurnResult(
                answer_text=(
                    "Запрос отклонён: обнаружены недопустимые управляющие инструкции. "
                    "Пожалуйста, сформулируйте вопрос о культурных мероприятиях Башкортостана обычным текстом."
                ),
                clarifying_question="Какие мероприятия, в каком городе и на какую дату вам подобрать?",
                fallback_used=True,
                fallback_reason="prompt_injection_detected",
                trace_identifier=trace_id,
                model_name=_FALLBACK_MODEL_NAME,
                prompt_version=self._resolve_prompt_version(),
            )
            return await self._persist_turn(
                result=injection_result,
                trace_identifier=trace_id,
                request_identifier=resolved_request_id,
                session_identifier=session_identifier,
                channel=channel,
                subscriber_identifier=subscriber_identifier,
                user_query=inspection.cleaned_text,
                reference_now=reference_now,
                started_at=started_at,
                plan_summary="Запрос отклонён защитой от инъекций инструкций.",
                save_dialog_messages=False,
            )

        session_state = await self._dialog_repository.get_session_state(
            session_identifier=session_identifier,
            channel=channel,
            subscriber_identifier=subscriber_identifier,
            message_limit=max(_MINIMUM_HISTORY_LIMIT, self._dialog_history_limit),
        )

        if session_state.active_pending_action is not None:
            pending_result = await self._try_handle_pending_action(
                pending_action=session_state.active_pending_action,
                cleaned_message=inspection.cleaned_text,
                session_identifier=session_identifier,
                channel=channel,
                subscriber_identifier=subscriber_identifier,
                reference_now=reference_now,
                trace_identifier=trace_id,
            )
            if pending_result is not None:
                return await self._persist_turn(
                    result=pending_result,
                    trace_identifier=trace_id,
                    request_identifier=resolved_request_id,
                    session_identifier=session_identifier,
                    channel=channel,
                    subscriber_identifier=subscriber_identifier,
                    user_query=inspection.cleaned_text,
                    reference_now=reference_now,
                    started_at=started_at,
                    plan_summary="Обработка двухшагового подтверждения изменяющего действия.",
                    save_dialog_messages=bool(inspection.cleaned_text),
                )

        fallback_reason = await self._check_model_availability_and_budget(
            subscriber_identifier=subscriber_identifier, reference_now=reference_now
        )
        if fallback_reason is not None:
            fallback_result = await self._fallback_search.handle_fallback(
                session_state=session_state,
                user_message=inspection.cleaned_text,
                current_datetime=reference_now,
                reason=fallback_reason,
                trace_identifier=trace_id,
                maximum_results=_MAXIMUM_RECOMMENDED_EVENTS,
            )
            return await self._persist_turn(
                result=fallback_result,
                trace_identifier=trace_id,
                request_identifier=resolved_request_id,
                session_identifier=session_identifier,
                channel=channel,
                subscriber_identifier=subscriber_identifier,
                user_query=inspection.cleaned_text,
                reference_now=reference_now,
                started_at=started_at,
                plan_summary=f"Резервный детерминированный поиск ({fallback_reason}).",
                save_dialog_messages=bool(inspection.cleaned_text),
            )

        assert self._agent_orchestrator is not None
        tool_callbacks = self._build_tool_callbacks(
            session_identifier=session_identifier, channel=channel, reference_now=reference_now
        )
        maximum_steps = (
            self._language_model_settings.MAX_AGENT_STEPS if self._language_model_settings is not None else 5
        )

        try:
            raw_turn = await self._agent_orchestrator.run_turn(
                session_state=session_state,
                user_message=inspection.cleaned_text,
                current_datetime=reference_now,
                tool_callbacks=tool_callbacks,
                maximum_steps=maximum_steps,
            )
            validated_turn = self._post_validate_turn_result(raw_turn)
            if contains_system_prompt_leak(validated_turn.answer_text):
                raise PromptInjectionDetectedError(
                    "В ответе модели обнаружены защищённые маркеры системного промпта.",
                    source_context="assistant_response",
                )
        except ApplicationException as error:
            reason = self._classify_fallback_reason(error)
            logger.warning(
                "Переключение в резервный поиск после ошибки оркестратора "
                "(request_id=%s, channel=%s, reason=%s, error_type=%s).",
                resolved_request_id,
                channel.value,
                reason,
                type(error).__name__,
            )
            fallback_result = await self._fallback_search.handle_fallback(
                session_state=session_state,
                user_message=inspection.cleaned_text,
                current_datetime=reference_now,
                reason=reason,
                trace_identifier=trace_id,
                maximum_results=_MAXIMUM_RECOMMENDED_EVENTS,
            )
            return await self._persist_turn(
                result=fallback_result,
                trace_identifier=trace_id,
                request_identifier=resolved_request_id,
                session_identifier=session_identifier,
                channel=channel,
                subscriber_identifier=subscriber_identifier,
                user_query=inspection.cleaned_text,
                reference_now=reference_now,
                started_at=started_at,
                plan_summary=f"Автоматический переход на резервный поиск ({reason}).",
                save_dialog_messages=bool(inspection.cleaned_text),
            )

        plan_summary = (
            f"ИИ-агент выполнил шагов инструментов: {len(validated_turn.tool_invocations)}."
            if validated_turn.tool_invocations
            else "Прямой ответ ИИ-агента без вызова инструментов."
        )
        return await self._persist_turn(
            result=validated_turn,
            trace_identifier=trace_id,
            request_identifier=resolved_request_id,
            session_identifier=session_identifier,
            channel=channel,
            subscriber_identifier=subscriber_identifier,
            user_query=inspection.cleaned_text,
            reference_now=reference_now,
            started_at=started_at,
            plan_summary=plan_summary,
            save_dialog_messages=bool(inspection.cleaned_text),
        )

    def _resolve_prompt_version(self) -> str:
        """Возвращает версию системного промпта из настроек или пустую строку."""
        return self._language_model_settings.PROMPT_VERSION if self._language_model_settings is not None else ""

    @staticmethod
    def _normalize_decision_text(cleaned_message: str) -> str:
        """Нормализует короткий ответ пользователя для распознавания подтверждения или отмены."""
        stripped = cleaned_message.strip().lower().strip(".!?,:;\"'«»")
        return " ".join(stripped.split())

    @staticmethod
    def _classify_fallback_reason(error: ApplicationException) -> str:
        """Преобразует исключение оркестратора в стабильный код причины резервного режима."""
        if isinstance(error, PromptInjectionDetectedError):
            return "prompt_injection_detected"
        if isinstance(error, HallucinatedEventError):
            return "hallucinated_event"
        return f"orchestrator_error:{type(error).__name__}"

    async def _check_model_availability_and_budget(
        self, *, subscriber_identifier: str, reference_now: datetime
    ) -> str | None:
        """Проверяет конфигурацию модели и суточные финансовые лимиты."""
        if (
            self._agent_orchestrator is None
            or self._language_model_settings is None
            or not self._language_model_settings.API_KEY.strip()
        ):
            return "model_not_configured"

        budget_status = await self._dialog_repository.get_usage_budget_status(
            subscriber_identifier=subscriber_identifier,
            target_date=reference_now.date(),
            user_daily_limit_rubles=self._language_model_settings.USER_DAILY_BUDGET_RUBLES,
            global_daily_limit_rubles=self._language_model_settings.GLOBAL_DAILY_BUDGET_RUBLES,
        )
        if budget_status.is_budget_exceeded:
            return "budget_exceeded"
        return None

    async def _try_handle_pending_action(
        self,
        *,
        pending_action: PendingAction,
        cleaned_message: str,
        session_identifier: str,
        channel: DialogChannel,
        subscriber_identifier: str,
        reference_now: datetime,
        trace_identifier: UUID,
    ) -> AgentTurnResult | None:
        """Обрабатывает подтверждение или отмену активного `PendingAction`, если текст является решением."""
        normalized_decision = self._normalize_decision_text(cleaned_message)
        if not normalized_decision:
            return None

        if normalized_decision in _CANCELLATION_PHRASES:
            try:
                await self._dialog_repository.cancel_pending_action(
                    action_identifier=pending_action.action_identifier, session_identifier=session_identifier
                )
                answer_text = "Хорошо, отложенное действие отменено. Можем продолжить подбор мероприятий."
            except PendingActionNotFoundError:
                answer_text = "Активный запрос на подтверждение уже отменён или завершён."

            return AgentTurnResult(
                answer_text=answer_text,
                trace_identifier=trace_identifier,
                model_name=_CONFIRMATION_MODEL_NAME,
                prompt_version=self._resolve_prompt_version(),
            )

        if normalized_decision not in _CONFIRMATION_PHRASES:
            return None

        try:
            consumed = await self._dialog_repository.consume_pending_action(
                action_identifier=pending_action.action_identifier,
                session_identifier=session_identifier,
                current_time=reference_now,
            )
        except PendingActionExpiredError:
            return AgentTurnResult(
                answer_text=(
                    "Срок действия запроса на подтверждение истёк. "
                    "Пожалуйста, повторите просьбу о подписке или обращении, если это ещё актуально."
                ),
                trace_identifier=trace_identifier,
                model_name=_CONFIRMATION_MODEL_NAME,
                prompt_version=self._resolve_prompt_version(),
            )
        except PendingActionNotFoundError:
            return AgentTurnResult(
                answer_text="Запрос на подтверждение не найден или уже был выполнен ранее.",
                trace_identifier=trace_identifier,
                model_name=_CONFIRMATION_MODEL_NAME,
                prompt_version=self._resolve_prompt_version(),
            )

        answer_text = await self._execute_confirmed_action(
            consumed=consumed, channel=channel, subscriber_identifier=subscriber_identifier
        )
        return AgentTurnResult(
            answer_text=answer_text,
            trace_identifier=trace_identifier,
            model_name=_CONFIRMATION_MODEL_NAME,
            prompt_version=self._resolve_prompt_version(),
        )

    async def _execute_confirmed_action(
        self, *, consumed: PendingAction, channel: DialogChannel, subscriber_identifier: str
    ) -> str:
        """Выполняет подтверждённую мутацию через `SubscriptionService`."""
        if consumed.action_type == "subscribe":
            raw_city = consumed.payload.get("city")
            city = raw_city if isinstance(raw_city, str) else None
            raw_category = consumed.payload.get("category")
            category: EventCategory | None = None
            if isinstance(raw_category, EventCategory):
                category = raw_category
            elif isinstance(raw_category, str) and raw_category.strip():
                try:
                    category = EventCategory(raw_category.strip())
                except ValueError:
                    category = None

            subscription = await self._subscription_service.subscribe(
                channel=channel, subscriber_identifier=subscriber_identifier, city=city, category=category
            )
            city_part = f"город «{subscription.city}»" if subscription.city else "все города Башкортостана"
            category_part = (
                f"категория «{_CATEGORY_DISPLAY_LABELS.get(subscription.category, subscription.category.value)}»"
                if subscription.category is not None
                else "все категории"
            )
            return f"Подписка оформлена ({city_part}, {category_part}). Мы будем присылать подходящие новинки афиши."

        if consumed.action_type == "save_feedback":
            raw_event_id = consumed.payload.get("event_identifier")
            event_identifier: UUID | None = None
            if isinstance(raw_event_id, UUID):
                event_identifier = raw_event_id
            elif isinstance(raw_event_id, str) and raw_event_id.strip():
                try:
                    event_identifier = UUID(raw_event_id.strip())
                except ValueError:
                    event_identifier = None

            raw_message = consumed.payload.get("message_text")
            message_text = raw_message if isinstance(raw_message, str) else ""
            raw_contact = consumed.payload.get("contact_hint")
            contact_hint = raw_contact if isinstance(raw_contact, str) else None

            try:
                await self._subscription_service.submit_feedback(
                    channel=channel,
                    message_text=message_text,
                    event_identifier=event_identifier,
                    contact_hint=contact_hint,
                )
            except (PromptInjectionDetectedError, EventNotFoundError, ValidationException) as error:
                return f"Не удалось сохранить обращение: {error.message}"

            return "Спасибо! Ваше обращение сохранено и передано модераторам афиши."

        return "Подтверждённое действие выполнено."

    def _build_tool_callbacks(
        self, *, session_identifier: str, channel: DialogChannel, reference_now: datetime
    ) -> AgentToolCallbacks:
        """Собирает семь прикладных обработчиков инструментов для ИИ-агента."""

        async def search_events_cb(search_filter: EventSearchFilter) -> EventSearchPage:
            return await self._search_service.search_events(search_filter=search_filter)

        async def get_event_cb(event_identifier: UUID) -> Event:
            return await self._search_service.get_event(event_identifier=event_identifier)

        async def find_similar_cb(
            event_identifier: UUID,
            city: str | None,
            date_from: datetime | None,
            date_to: datetime | None,
            only_free: bool,
            limit: int,
        ) -> tuple[Event, ...]:
            target_event = await self._search_service.get_event(event_identifier=event_identifier)
            resolved_city: str | None = target_event.city
            if city is not None and city.strip():
                resolved_city = await resolve_normalized_city(city.strip(), event_repository=self._event_repository)
            return await self._event_repository.find_similar_events(
                event_identifier=target_event.identifier,
                city=resolved_city,
                date_from=date_from,
                date_to=date_to,
                near_center=only_free,
                limit=limit,
            )

        async def get_venue_schedule_cb(
            venue: str, date_from: datetime | None, date_to: datetime | None, limit: int
        ) -> tuple[Event, ...]:
            page = await self._search_service.get_venue_schedule(
                venue=venue, date_from=date_from, date_to=date_to, page=1, page_size=limit
            )
            return page.items

        async def plan_weekend_cb(request: WeekendPlanRequest) -> WeekendPlanResult:
            return await self._weekend_planner.plan_weekend(request)

        async def prepare_subscribe_cb(city: str | None, category: EventCategory | None) -> PendingAction:
            cleaned_city = normalize_optional_text(city)
            resolved_city = (
                await resolve_normalized_city(cleaned_city, event_repository=self._event_repository)
                if cleaned_city is not None
                else None
            )
            city_label = resolved_city or "все города Башкортостана"
            category_label = (
                _CATEGORY_DISPLAY_LABELS.get(category, category.value) if category is not None else "все категории"
            )
            pending = PendingAction(
                action_identifier=uuid4(),
                session_identifier=session_identifier,
                channel=channel,
                action_type="subscribe",
                payload={"city": resolved_city, "category": category.value if category is not None else None},
                confirmation_prompt=(
                    f"Подтвердить оформление подписки на уведомления ({city_label}, {category_label})? "
                    "Ответьте «Да» для подтверждения или «Отмена»."
                ),
                created_at=reference_now,
                expires_at=reference_now + timedelta(seconds=self._pending_action_ttl_seconds),
            )
            return await self._dialog_repository.create_pending_action(pending_action=pending)

        async def prepare_feedback_cb(
            event_identifier: UUID | None, message_text: str, contact_hint: str | None
        ) -> PendingAction:
            msg_inspection = inspect_untrusted_text(message_text, maximum_length=2000)
            if msg_inspection.has_suspicious_instructions:
                raise PromptInjectionDetectedError(
                    "Обращение отклонено: обнаружены подозрительные управляющие инструкции.",
                    source_context="feedback_message",
                    matched_patterns=msg_inspection.matched_patterns,
                )
            cleaned_message = normalize_optional_text(msg_inspection.cleaned_text)
            if cleaned_message is None:
                raise ValidationException("Текст обращения не может быть пустым.")

            cleaned_contact: str | None = None
            normalized_contact = normalize_optional_text(contact_hint)
            if normalized_contact is not None:
                contact_inspection = inspect_untrusted_text(normalized_contact, maximum_length=255)
                if contact_inspection.has_suspicious_instructions:
                    raise PromptInjectionDetectedError(
                        "Контактная подсказка отклонена: обнаружены подозрительные управляющие инструкции.",
                        source_context="feedback_contact",
                        matched_patterns=contact_inspection.matched_patterns,
                    )
                cleaned_contact = normalize_optional_text(contact_inspection.cleaned_text)

            if event_identifier is not None:
                await self._search_service.get_event(event_identifier=event_identifier)

            preview = sanitize_control_characters(cleaned_message, maximum_length=100)
            pending = PendingAction(
                action_identifier=uuid4(),
                session_identifier=session_identifier,
                channel=channel,
                action_type="save_feedback",
                payload={
                    "event_identifier": str(event_identifier) if event_identifier is not None else None,
                    "message_text": cleaned_message,
                    "contact_hint": cleaned_contact,
                },
                confirmation_prompt=(
                    f"Подтвердить отправку обращения модераторам: «{preview}»? "
                    "Ответьте «Да» для подтверждения или «Отмена»."
                ),
                created_at=reference_now,
                expires_at=reference_now + timedelta(seconds=self._pending_action_ttl_seconds),
            )
            return await self._dialog_repository.create_pending_action(pending_action=pending)

        return AgentToolCallbacks(
            search_events=search_events_cb,
            get_event=get_event_cb,
            find_similar=find_similar_cb,
            get_venue_schedule=get_venue_schedule_cb,
            plan_weekend=plan_weekend_cb,
            prepare_subscribe=prepare_subscribe_cb,
            prepare_feedback=prepare_feedback_cb,
        )

    @staticmethod
    def _post_validate_turn_result(turn_result: AgentTurnResult) -> AgentTurnResult:
        """Проверяет обоснованность карточек и пояснений результатами инструментов текущего хода."""
        allowed_ids: set[UUID] = {
            event_id
            for invocation in turn_result.tool_invocations
            if invocation.succeeded
            for event_id in invocation.returned_event_identifiers
        }

        verified_events: list[Event] = []
        seen_ids: set[UUID] = set()
        for event in turn_result.recommended_events:
            if event.identifier in allowed_ids and event.identifier not in seen_ids:
                seen_ids.add(event.identifier)
                verified_events.append(event)
                if len(verified_events) >= _MAXIMUM_RECOMMENDED_EVENTS:
                    break

        verified_ids = {event.identifier for event in verified_events}
        verified_explanations: dict[UUID, str] = {
            event_id: explanation
            for event_id, explanation in turn_result.explanations_by_event.items()
            if event_id in verified_ids and explanation.strip()
        }

        return replace(
            turn_result, recommended_events=tuple(verified_events), explanations_by_event=verified_explanations
        )

    async def _persist_turn(
        self,
        *,
        result: AgentTurnResult,
        trace_identifier: UUID,
        request_identifier: str,
        session_identifier: str,
        channel: DialogChannel,
        subscriber_identifier: str,
        user_query: str,
        reference_now: datetime,
        started_at: float,
        plan_summary: str,
        save_dialog_messages: bool,
    ) -> AgentTurnResult:
        """Сохраняет реплики диалога, учёт токенов/стоимости и запись трассировки."""
        if save_dialog_messages:
            assistant_timestamp = attach_default_timezone(datetime.now())
            if assistant_timestamp < reference_now:
                assistant_timestamp = reference_now
            messages = (
                DialogMessage(role="user", content=user_query, created_at=reference_now),
                DialogMessage(role="assistant", content=result.answer_text, created_at=assistant_timestamp),
            )
            await self._dialog_repository.append_messages(session_identifier=session_identifier, messages=messages)

        if result.tokens_input + result.tokens_output > 0:
            await self._dialog_repository.record_model_usage(
                subscriber_identifier=subscriber_identifier,
                target_date=reference_now.date(),
                cost_rubles=result.cost_rubles,
                tokens_input=result.tokens_input,
                tokens_output=result.tokens_output,
            )

        elapsed_ms = max(result.latency_milliseconds, int((time.perf_counter() - started_at) * 1000))
        grounded_ids = tuple(
            dict.fromkeys(
                [
                    *(event.identifier for event in result.recommended_events),
                    *(
                        event_id
                        for invocation in result.tool_invocations
                        if invocation.succeeded
                        for event_id in invocation.returned_event_identifiers
                    ),
                ]
            )
        )
        prompt_version = result.prompt_version or self._resolve_prompt_version()

        trace_record = AgentTraceRecord(
            trace_identifier=trace_identifier,
            request_identifier=request_identifier,
            session_identifier=session_identifier,
            channel=channel,
            user_query=user_query,
            plan_summary=plan_summary,
            tool_invocations=result.tool_invocations,
            grounded_event_identifiers=grounded_ids,
            fallback_used=result.fallback_used,
            latency_milliseconds=elapsed_ms,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_rubles=result.cost_rubles,
            model_name=result.model_name,
            prompt_version=prompt_version,
            created_at=reference_now,
        )
        await self._dialog_repository.save_trace(trace=trace_record)

        return replace(
            result, trace_identifier=trace_identifier, latency_milliseconds=elapsed_ms, prompt_version=prompt_version
        )

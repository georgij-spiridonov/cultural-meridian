"""Прикладной конвейер сбора, гибридной нормализации, дедупликации и проверки событий.

Архитектурное решение:
    Слой сбора данных изолирован от конкретных протоколов и сетевых библиотек:
    загрузка элементов, проверка доступности ссылок, хранение сырых записей,
    обращение к языковой модели для сложных описаний и сохранение каталога
    выполняются исключительно через Protocol-порты доменного слоя.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from src.application._helpers import attach_default_timezone, resolve_duration_minutes
from src.application.subscriptions import SubscriptionService
from src.core.exceptions import ApplicationException, ValidationException
from src.core.logger import get_logger
from src.core.types import EventCategory, EventStatus, RealCollection, SourceType
from src.domain.entities import (
    AuditLogEntry,
    CityReference,
    Event,
    EventSearchFilter,
    EventSourceReference,
    ExtractedEventFields,
    FetchedSourceItem,
    IngestionReport,
    LinkCheckResult,
    ModerationItem,
    RawEventRecord,
    SourceDefinition,
    VenueReference,
)
from src.domain.ports import (
    AdministrationRepositoryPort,
    EventRepositoryPort,
    LinkCheckerPort,
    RawEventRepositoryPort,
    RawNormalizationStatus,
    SourceFetcherPort,
    StructuredExtractorPort,
)
from src.domain.services import (
    BASHKORTOSTAN_TIMEZONE,
    are_events_duplicates,
    compute_deduplication_key,
    compute_payload_hash,
    inspect_untrusted_text,
    merge_event_sources,
    normalize_city,
    normalize_optional_text,
    normalize_source_item,
    normalize_title,
    normalize_venue,
    sanitize_control_characters,
)

__all__ = ["EventIngestionService"]

logger = get_logger(__name__)

_INGESTION_ACTOR: Final[str] = "ingestion_worker"
_FALLBACK_CITY: Final[str] = "Уточняется"
_FALLBACK_VENUE: Final[str] = "Уточняется на странице источника"
_FALLBACK_TITLE: Final[str] = "Мероприятие без названия"
_FALLBACK_SOURCE_URL: Final[str] = "https://cultural-meridian.local/unknown"
_DEDUPLICATION_WINDOW_HOURS: Final[int] = 24
_MAXIMUM_SEARCH_CANDIDATES: Final[int] = 100
_VERIFY_LINKS_PAGE_SIZE: Final[int] = 100
_ALLOWED_AGE_LIMITS: Final[frozenset[int]] = frozenset({0, 6, 12, 16, 18})
_CANCELED_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    {"canceled", "cancelled", "отменено", "отмена", "отменен", "отменён"}
)
_CANCELED_TITLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:\[\s*(?:отменен[оа]?|отменён[оа]?|canceled|cancelled)\s*\]"
    r"|\(\s*(?:отменен[оа]?|отменён[оа]?|canceled|cancelled)\s*\)"
    r"|\b(?:отменено|отменён|отменен|canceled|cancelled)\b)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _SanitizedSourceData:
    """Промежуточный результат очистки и проверки текстов сырого элемента."""

    item: FetchedSourceItem
    cleaned_description: str
    has_suspicious_instructions: bool
    matched_injection_patterns: tuple[str, ...]


@dataclass(slots=True)
class _IngestionCounters:
    """Счётчики и диагностика одного прогона конвейера сбора."""

    sources_processed: int = 0
    sources_failed: int = 0
    items_fetched: int = 0
    items_published: int = 0
    items_sent_to_moderation: int = 0
    items_deduplicated: int = 0
    items_unchanged: int = 0
    notifications_triggered: int = 0
    errors: list[str] | None = None

    def add_error(self, message: str) -> None:
        """Добавляет диагностическое сообщение об ошибке источника или обработки."""
        if self.errors is None:
            self.errors = []
        self.errors.append(message)

    def to_report(self) -> IngestionReport:
        """Формирует неизменяемый отчёт по итогам прогона."""
        return IngestionReport(
            sources_processed=self.sources_processed,
            sources_failed=self.sources_failed,
            items_fetched=self.items_fetched,
            items_published=self.items_published,
            items_sent_to_moderation=self.items_sent_to_moderation,
            items_deduplicated=self.items_deduplicated,
            items_unchanged=self.items_unchanged,
            errors=tuple(self.errors or ()),
        )


class EventIngestionService:
    """Прикладной конвейер сбора, гибридной нормализации, дедупликации и проверки событий."""

    def __init__(
        self,
        *,
        source_fetcher: SourceFetcherPort,
        link_checker: LinkCheckerPort,
        raw_event_repository: RawEventRepositoryPort,
        event_repository: EventRepositoryPort,
        administration_repository: AdministrationRepositoryPort,
        structured_extractor: StructuredExtractorPort | None = None,
        subscription_service: SubscriptionService | None = None,
        confidence_threshold: float = 0.75,
        link_check_timeout_seconds: float = 4.0,
    ) -> None:
        """Инициализирует сервис сбора и верификации внешних источников.

        Arguments:
            source_fetcher: Порт загрузки сырых элементов из источников (RSS, API, HTML).
            link_checker: Порт проверки доступности ссылок на первоисточники.
            raw_event_repository: Порт хранения сырых записей для идемпотентности и переигрывания.
            event_repository: Порт каталога нормализованных мероприятий и справочников.
            administration_repository: Порт очереди модерации и журнала изменений.
            structured_extractor: Необязательный порт гибридного извлечения полей через языковую модель.
            subscription_service: Необязательный сервис уведомлений по подпискам при публикации.
            confidence_threshold: Минимальный порог уверенности нормализации для автопубликации.
            link_check_timeout_seconds: Тайм-аут проверки доступности ссылки в секундах.

        Raises:
            ValidationException: Если числовые параметры конфигурации выходят за допустимые границы.
        """
        if not (0.0 <= confidence_threshold <= 1.0):
            raise ValidationException("Порог уверенности нормализации должен находиться в диапазоне от 0.0 до 1.0.")
        if link_check_timeout_seconds <= 0.0:
            raise ValidationException("Тайм-аут проверки ссылок должен быть положительным числом.")

        self._source_fetcher = source_fetcher
        self._link_checker = link_checker
        self._raw_event_repository = raw_event_repository
        self._event_repository = event_repository
        self._administration_repository = administration_repository
        self._structured_extractor = structured_extractor
        self._subscription_service = subscription_service
        self._confidence_threshold = confidence_threshold
        self._link_check_timeout_seconds = link_check_timeout_seconds

    async def ingest_sources(
        self, *, sources: Sequence[SourceDefinition], current_time: datetime | None = None
    ) -> IngestionReport:
        """Выполняет сбор, сохранение сырых записей, нормализацию и публикацию из активных источников.

        Сбой одного источника изолируется и отражается в счётчике `sources_failed` и списке
        ошибок без остановки обработки остальных источников. Повторная загрузка неизменённых
        нормализованных записей пропускается по паре `(source_type, source_hash)`.

        Arguments:
            sources: Последовательность описаний источников из реестра конфигурации.
            current_time: Необязательная опорная метка времени для детерминированных прогонов.

        Returns:
            Сводный отчёт `IngestionReport` по всем опрошенным источникам.
        """
        effective_time = _resolve_current_time(current_time)
        enabled_sources = [source for source in sources if source.is_enabled]
        counters = _IngestionCounters()
        if not enabled_sources:
            return counters.to_report()

        cities = await self._event_repository.list_cities()
        venues = await self._event_repository.list_venues()
        batch_events_by_identifier: dict[UUID, Event] = {}
        batch_events_by_hash: dict[str, Event] = {}

        for source in enabled_sources:
            try:
                fetched_items = await self._source_fetcher.fetch_items(source=source)
            except ApplicationException as error:
                logger.warning("Ошибка загрузки источника «%s»: %s", source.name, error)
                counters.sources_failed += 1
                counters.add_error(f"{source.name}: {error}")
                continue

            counters.sources_processed += 1
            counters.items_fetched += len(fetched_items)

            for fetched_item in fetched_items:
                source_hash = _compute_fetched_item_hash(fetched_item)
                raw_payload = _build_raw_storage_payload(fetched_item)
                raw_record = RawEventRecord(
                    identifier=uuid4(),
                    source_name=fetched_item.source_name,
                    source_type=fetched_item.source_type,
                    external_identifier=fetched_item.external_identifier,
                    source_url=fetched_item.source_url,
                    source_hash=source_hash,
                    raw_payload=raw_payload,
                    fetched_at=effective_time,
                    normalization_status="pending",
                )
                saved_raw_record, is_new = await self._raw_event_repository.save_raw_event(raw_event=raw_record)

                if not is_new and saved_raw_record.normalization_status == "normalized":
                    existing_event = batch_events_by_hash.get(saved_raw_record.source_hash)
                    if existing_event is None:
                        existing_event = await self._event_repository.find_by_source_hash(
                            source_type=saved_raw_record.source_type, source_hash=saved_raw_record.source_hash
                        )
                    if existing_event is not None:
                        batch_events_by_hash[saved_raw_record.source_hash] = existing_event
                        batch_events_by_identifier[existing_event.identifier] = existing_event
                        counters.items_unchanged += 1
                        continue

                await self._process_raw_record(
                    raw_record=saved_raw_record,
                    fetched_item=fetched_item,
                    source_definition=source,
                    cities=cities,
                    venues=venues,
                    current_time=effective_time,
                    counters=counters,
                    batch_events_by_identifier=batch_events_by_identifier,
                    batch_events_by_hash=batch_events_by_hash,
                )

        if counters.notifications_triggered > 0:
            await self._dispatch_pending_notifications_safely(current_time=effective_time)

        return counters.to_report()

    async def reprocess_raw_events(
        self,
        *,
        source_name: str | None = None,
        normalization_status: RawNormalizationStatus | None = "pending",
        limit: int = 100,
        current_time: datetime | None = None,
        sources_by_name: Mapping[str, SourceDefinition] | None = None,
    ) -> IngestionReport:
        """Переигрывает нормализацию сохранённых сырых записей без обращения к внешним сайтам.

        Arguments:
            source_name: Необязательный фильтр по имени источника.
            normalization_status: Фильтр по статусу обработки сырой записи (`pending`, `failed`, `normalized`).
            limit: Максимальное количество сырых записей для одного прохода.
            current_time: Необязательная опорная метка времени для разрешения относительных дат.
            sources_by_name: Необязательный справочник описаний источников для `default_city`/`default_category`.

        Returns:
            Сводный отчёт `IngestionReport` по результатам повторной нормализации.

        Raises:
            ValidationException: Если `limit` меньше 1.
        """
        if limit < 1:
            raise ValidationException("Лимит выборки сырых записей должен быть положительным числом.")

        effective_time = _resolve_current_time(current_time)
        raw_records = await self._raw_event_repository.list_raw_events_for_reprocessing(
            source_name=source_name, normalization_status=normalization_status, limit=limit
        )
        counters = _IngestionCounters()
        if not raw_records:
            return counters.to_report()

        cities = await self._event_repository.list_cities()
        venues = await self._event_repository.list_venues()
        batch_events_by_identifier: dict[UUID, Event] = {}
        batch_events_by_hash: dict[str, Event] = {}

        counters.sources_processed = len({record.source_name for record in raw_records})
        counters.items_fetched = len(raw_records)

        for raw_record in raw_records:
            source_definition = sources_by_name.get(raw_record.source_name) if sources_by_name is not None else None
            fetched_item = _fetched_item_from_raw_record(raw_record)
            await self._process_raw_record(
                raw_record=raw_record,
                fetched_item=fetched_item,
                source_definition=source_definition,
                cities=cities,
                venues=venues,
                current_time=effective_time,
                counters=counters,
                batch_events_by_identifier=batch_events_by_identifier,
                batch_events_by_hash=batch_events_by_hash,
            )

        if counters.notifications_triggered > 0:
            await self._dispatch_pending_notifications_safely(current_time=effective_time)

        return counters.to_report()

    async def verify_published_links(self, *, limit: int = 200, current_time: datetime | None = None) -> int:
        """Проверяет доступность ссылок опубликованных карточек и снимает битые в модерацию.

        Arguments:
            limit: Максимальное число опубликованных карточек для проверки за один проход.
            current_time: Необязательная опорная метка времени для штампов обновления и аудита.

        Returns:
            Количество карточек, переведённых из `PUBLISHED` в `DRAFT` из-за недоступной ссылки.

        Raises:
            ValidationException: Если `limit` меньше 1.
        """
        if limit < 1:
            raise ValidationException("Лимит проверки ссылок должен быть положительным числом.")

        effective_time = _resolve_current_time(current_time)
        events_to_check = await self._collect_published_events(limit=limit)
        demoted_count = 0

        for published_event in events_to_check:
            link_result = await self._safe_check_link(
                target_url=published_event.source_url, current_time=effective_time
            )
            if link_result.is_reachable:
                continue

            demoted_event = replace(
                published_event, status=EventStatus.DRAFT, verified_at=link_result.checked_at, updated_at=effective_time
            )
            saved_event = await self._event_repository.save_event(event=demoted_event)

            reason_detail = _format_link_failure_detail(link_result)
            await self._administration_repository.enqueue_moderation_item(
                moderation_item=ModerationItem(
                    moderation_identifier=uuid4(),
                    event_identifier=saved_event.identifier,
                    reason=f"Ссылка на первоисточник стала недоступной ({reason_detail}).",
                    status="pending",
                    has_suspicious_instructions=False,
                    matched_injection_patterns=(),
                    created_at=effective_time,
                )
            )
            await self._administration_repository.append_audit_log(
                entry=AuditLogEntry(
                    entry_identifier=uuid4(),
                    actor=_INGESTION_ACTOR,
                    action="demote_broken_link",
                    entity_type="event",
                    entity_identifier=str(saved_event.identifier),
                    changes={
                        "previous_status": published_event.status.value,
                        "new_status": EventStatus.DRAFT.value,
                        "source_url": published_event.source_url,
                        "http_status_code": link_result.http_status_code,
                        "error_reason": link_result.error_reason,
                    },
                    created_at=effective_time,
                )
            )
            demoted_count += 1

        return demoted_count

    async def _collect_published_events(self, *, limit: int) -> list[Event]:
        """Собирает до `limit` опубликованных карточек постранично до мутации статусов."""
        collected: list[Event] = []
        page_number = 1
        page_size = min(_VERIFY_LINKS_PAGE_SIZE, limit)

        while len(collected) < limit:
            search_page = await self._event_repository.search_events(
                search_filter=EventSearchFilter(page=page_number, page_size=page_size),
                statuses=(EventStatus.PUBLISHED,),
            )
            if not search_page.items:
                break
            remaining = limit - len(collected)
            collected.extend(search_page.items[:remaining])
            if not search_page.has_more:
                break
            page_number += 1

        return collected

    async def _process_raw_record(
        self,
        *,
        raw_record: RawEventRecord,
        fetched_item: FetchedSourceItem,
        source_definition: SourceDefinition | None,
        cities: RealCollection[CityReference],
        venues: RealCollection[VenueReference],
        current_time: datetime,
        counters: _IngestionCounters,
        batch_events_by_identifier: dict[UUID, Event],
        batch_events_by_hash: dict[str, Event],
    ) -> None:
        """Выполняет единый 9-шаговый конвейер нормализации, дедупликации и сохранения."""
        try:
            saved_event = await self._run_normalization_pipeline(
                raw_record=raw_record,
                fetched_item=fetched_item,
                source_definition=source_definition,
                cities=cities,
                venues=venues,
                current_time=current_time,
                counters=counters,
                batch_events_by_identifier=batch_events_by_identifier,
                batch_events_by_hash=batch_events_by_hash,
            )
        except ApplicationException as error:
            logger.warning(
                "Ошибка обработки сырой записи %s (%s): %s",
                raw_record.identifier,
                raw_record.external_identifier,
                error,
            )
            counters.add_error(f"{raw_record.source_name}/{raw_record.external_identifier}: {error}")
            await self._safe_update_raw_status(
                raw_event_identifier=raw_record.identifier, normalization_status="failed", error_message=str(error)
            )
            return

        batch_events_by_identifier[saved_event.identifier] = saved_event
        batch_events_by_hash[raw_record.source_hash] = saved_event
        for source_reference in saved_event.sources:
            batch_events_by_hash[source_reference.source_hash] = saved_event

        await self._safe_update_raw_status(
            raw_event_identifier=raw_record.identifier, normalization_status="normalized", error_message=None
        )

    async def _run_normalization_pipeline(
        self,
        *,
        raw_record: RawEventRecord,
        fetched_item: FetchedSourceItem,
        source_definition: SourceDefinition | None,
        cities: RealCollection[CityReference],
        venues: RealCollection[VenueReference],
        current_time: datetime,
        counters: _IngestionCounters,
        batch_events_by_identifier: Mapping[UUID, Event],
        batch_events_by_hash: Mapping[str, Event],
    ) -> Event:
        # 1. Проверка и очистка недоверенных текстовых полей
        sanitized = _sanitize_fetched_item(fetched_item)

        # 2–3. Детерминированная нормализация с опорой на кэшированные справочники
        default_city = source_definition.default_city if source_definition is not None else None
        default_category = source_definition.default_category if source_definition is not None else None
        extracted = normalize_source_item(
            sanitized.item,
            reference_datetime=current_time,
            cities=cities,
            venues=venues,
            default_city=default_city,
            default_category=default_category,
        )

        # 4. Гибридная нормализация сложного или неполного описания через быструю модель
        extractor_error_reason: str | None = None
        needs_hybrid_extraction = (
            extracted.confidence < self._confidence_threshold or extracted.date_start is None or extracted.city is None
        )
        if needs_hybrid_extraction and self._structured_extractor is not None:
            extracted, extractor_error_reason = await self._apply_structured_extractor(
                sanitized_item=sanitized.item,
                deterministic=extracted,
                cities=cities,
                venues=venues,
                current_time=current_time,
            )

        # 5. Проверка отмены мероприятия по сырым метаданным или заголовку
        is_canceled = _is_event_canceled(
            raw_payload=raw_record.raw_payload, raw_title=sanitized.item.raw_title, extracted_title=extracted.title
        )

        # 6. Проверка доступности ссылки и актуальности даты начала
        effective_source_url = (
            normalize_optional_text(sanitized.item.source_url)
            or normalize_optional_text(raw_record.source_url)
            or _FALLBACK_SOURCE_URL
        )[:1024]
        link_result = await self._safe_check_link(target_url=effective_source_url, current_time=current_time)

        has_real_title = _has_valid_title(extracted.title)
        effective_title = (normalize_optional_text(extracted.title) or _FALLBACK_TITLE)[:500]
        effective_normalized_title = (normalize_title(effective_title) or normalize_title(_FALLBACK_TITLE))[:500]
        has_real_city = extracted.city is not None
        effective_city = (extracted.city or _FALLBACK_CITY)[:120]
        has_real_date = extracted.date_start is not None
        effective_date_start = (
            attach_default_timezone(extracted.date_start)
            if extracted.date_start is not None
            else current_time + timedelta(days=1)
        )
        effective_date_end = (
            attach_default_timezone(extracted.date_end)
            if extracted.date_end is not None and attach_default_timezone(extracted.date_end) >= effective_date_start
            else None
        )
        effective_duration = _safe_resolve_duration(
            date_start=effective_date_start, date_end=effective_date_end, explicit_duration=extracted.duration_minutes
        )
        effective_venue = (extracted.venue or _FALLBACK_VENUE)[:255]
        effective_address = (extracted.address or "")[:500]
        latitude, longitude = _resolve_coordinates(
            effective_venue=effective_venue,
            effective_city=effective_city,
            venues=venues,
            raw_payload=raw_record.raw_payload,
        )
        is_past_event = (
            False
            if is_canceled or not has_real_date
            else _is_event_in_past(
                date_start=effective_date_start, date_end=effective_date_end, current_time=current_time
            )
        )

        # 7. Дедупликация по source_hash и нечёткому совпадению (город, дата, название, площадка)
        deduplication_key = compute_deduplication_key(
            normalized_title=effective_normalized_title,
            date_start=effective_date_start,
            city=effective_city,
            venue=extracted.venue,
        )
        existing_event, matched_by_hash = await self._find_existing_event(
            source_type=raw_record.source_type,
            source_hash=raw_record.source_hash,
            normalized_title=effective_normalized_title,
            date_start=effective_date_start,
            city=effective_city,
            venue=extracted.venue,
            deduplication_key=deduplication_key,
            can_search_duplicates=has_real_city and has_real_date,
            batch_events_by_identifier=batch_events_by_identifier,
            batch_events_by_hash=batch_events_by_hash,
        )

        # 8. Принятие решения: CANCELED / PUBLISHED / DRAFT + модерация
        moderation_reasons = _collect_moderation_reasons(
            is_canceled=is_canceled,
            has_suspicious_instructions=sanitized.has_suspicious_instructions,
            link_result=link_result,
            is_past_event=is_past_event,
            has_real_title=has_real_title,
            has_real_city=has_real_city,
            has_real_date=has_real_date,
            confidence=extracted.confidence,
            confidence_threshold=self._confidence_threshold,
            extractor_error_reason=extractor_error_reason,
        )

        if is_canceled:
            decision_status = EventStatus.CANCELED
        elif not moderation_reasons:
            decision_status = EventStatus.PUBLISHED
        else:
            decision_status = EventStatus.DRAFT

        source_reference = EventSourceReference(
            source_name=raw_record.source_name[:120],
            source_type=raw_record.source_type,
            source_url=effective_source_url,
            source_hash=raw_record.source_hash,
            verified_at=link_result.checked_at if link_result.is_reachable else None,
        )
        price_minimum, price_maximum, is_free = _normalize_pricing_constraints(
            price_minimum=extracted.price_minimum, price_maximum=extracted.price_maximum, is_free=extracted.is_free
        )
        age_limit = extracted.age_limit if extracted.age_limit in _ALLOWED_AGE_LIMITS else 0
        language = _normalize_language_value(extracted.language)
        organization_link = _normalize_organization_link(sanitized.item.organization_link)
        confidence = round(min(max(extracted.confidence, 0.0), 1.0), 4)
        category = extracted.category or default_category or EventCategory.OTHER

        normalized_fields = ExtractedEventFields(
            title=effective_title,
            category=category,
            date_start=effective_date_start,
            date_end=effective_date_end,
            duration_minutes=effective_duration,
            city=effective_city,
            venue=effective_venue,
            address=effective_address,
            price_minimum=price_minimum,
            price_maximum=price_maximum,
            is_free=is_free,
            age_limit=age_limit,
            language=language,
            tags=extracted.tags,
            confidence=confidence,
        )

        # 9. Сохранение карточки, очереди модерации и журнала изменений
        if existing_event is None:
            candidate_event = Event(
                identifier=uuid4(),
                title=effective_title,
                normalized_title=effective_normalized_title,
                category=category,
                tags=extracted.tags,
                date_start=effective_date_start,
                date_end=effective_date_end,
                duration_minutes=effective_duration,
                city=effective_city,
                venue=effective_venue,
                address=effective_address,
                latitude=latitude,
                longitude=longitude,
                description=sanitized.cleaned_description,
                price_minimum=price_minimum,
                price_maximum=price_maximum,
                is_free=is_free,
                age_limit=age_limit,
                language=language,
                organization_link=organization_link,
                source_url=effective_source_url,
                source_type=raw_record.source_type,
                source_hash=raw_record.source_hash,
                verified_at=source_reference.verified_at,
                confidence=confidence,
                status=decision_status,
                deduplication_key=deduplication_key,
                created_at=current_time,
                updated_at=current_time,
                sources=(source_reference,),
            )
            saved_event = await self._event_repository.save_event(event=candidate_event)
            audit_action = "cancel" if decision_status is EventStatus.CANCELED else "create"
            if decision_status is EventStatus.PUBLISHED:
                counters.items_published += 1
            elif decision_status is EventStatus.DRAFT:
                await self._enqueue_moderation(
                    event=saved_event,
                    raw_record=raw_record,
                    reasons=moderation_reasons,
                    has_suspicious_instructions=sanitized.has_suspicious_instructions,
                    matched_injection_patterns=sanitized.matched_injection_patterns,
                    current_time=current_time,
                )
                counters.items_sent_to_moderation += 1
        elif matched_by_hash:
            updated_event = _build_reprocessed_same_source_event(
                existing_event=existing_event,
                source_reference=source_reference,
                fields=normalized_fields,
                normalized_title=effective_normalized_title,
                description=sanitized.cleaned_description,
                latitude=latitude,
                longitude=longitude,
                organization_link=organization_link,
                deduplication_key=deduplication_key,
                status=decision_status,
                updated_at=current_time,
            )
            saved_event = await self._event_repository.save_event(event=updated_event)
            audit_action = "cancel" if decision_status is EventStatus.CANCELED else "update"
            if decision_status is EventStatus.PUBLISHED:
                counters.items_published += 1
            elif decision_status is EventStatus.DRAFT:
                await self._enqueue_moderation(
                    event=saved_event,
                    raw_record=raw_record,
                    reasons=moderation_reasons,
                    has_suspicious_instructions=sanitized.has_suspicious_instructions,
                    matched_injection_patterns=sanitized.matched_injection_patterns,
                    current_time=current_time,
                )
                counters.items_sent_to_moderation += 1
        else:
            merged_status = _resolve_merged_status(
                existing_status=existing_event.status,
                is_canceled=is_canceled,
                has_suspicious_instructions=sanitized.has_suspicious_instructions,
            )
            merged_event = merge_event_sources(
                existing_event,
                new_source=source_reference,
                updated_fields=normalized_fields,
                updated_at=current_time,
                new_status=merged_status,
            )
            saved_event = await self._event_repository.save_event(event=merged_event)
            counters.items_deduplicated += 1
            audit_action = "cancel" if merged_status is EventStatus.CANCELED else "update"
            if sanitized.has_suspicious_instructions:
                await self._enqueue_moderation(
                    event=saved_event,
                    raw_record=raw_record,
                    reasons=moderation_reasons,
                    has_suspicious_instructions=True,
                    matched_injection_patterns=sanitized.matched_injection_patterns,
                    current_time=current_time,
                )

        await self._administration_repository.append_audit_log(
            entry=AuditLogEntry(
                entry_identifier=uuid4(),
                actor=_INGESTION_ACTOR,
                action=audit_action,
                entity_type="event",
                entity_identifier=str(saved_event.identifier),
                changes={
                    "source_name": raw_record.source_name,
                    "source_type": raw_record.source_type.value,
                    "source_url": effective_source_url,
                    "status": saved_event.status.value,
                    "confidence": saved_event.confidence,
                    "deduplicated": existing_event is not None and not matched_by_hash,
                    "moderation_reasons": list(moderation_reasons),
                },
                created_at=current_time,
            )
        )
        was_newly_published = (
            existing_event is None or existing_event.status is not EventStatus.PUBLISHED
        ) and saved_event.status is EventStatus.PUBLISHED
        if was_newly_published and await self._enqueue_event_notifications_safely(
            event=saved_event, current_time=current_time
        ):
            counters.notifications_triggered += 1
        return saved_event

    async def _enqueue_event_notifications_safely(self, *, event: Event, current_time: datetime) -> bool:
        if self._subscription_service is None:
            return False
        try:
            await self._subscription_service.notify_subscribers_for_event(
                event=event, dispatch_immediately=False, current_time=current_time
            )
            return True
        except Exception as error:
            logger.warning(
                "Не удалось поставить в очередь уведомления по подпискам для события %s: %s", event.identifier, error
            )
            return False

    async def _dispatch_pending_notifications_safely(self, *, current_time: datetime) -> None:
        if self._subscription_service is None:
            return
        try:
            await self._subscription_service.dispatch_pending_telegram_notifications(current_time=current_time)
        except Exception as error:
            logger.warning("Не удалось выполнить рассылку Telegram-уведомлений после сбора событий: %s", error)

    async def _apply_structured_extractor(
        self,
        *,
        sanitized_item: FetchedSourceItem,
        deterministic: ExtractedEventFields,
        cities: RealCollection[CityReference],
        venues: RealCollection[VenueReference],
        current_time: datetime,
    ) -> tuple[ExtractedEventFields, str | None]:
        assert self._structured_extractor is not None
        try:
            llm_fields = await self._structured_extractor.extract_event_fields(
                raw_title=sanitized_item.raw_title,
                raw_description=sanitized_item.raw_description,
                raw_date_text=sanitized_item.raw_date_text,
                raw_price_text=sanitized_item.raw_price_text,
                raw_city=sanitized_item.raw_city,
                raw_venue=sanitized_item.raw_venue,
                raw_category=sanitized_item.raw_category,
                raw_age_text=sanitized_item.raw_age_text,
                reference_datetime=current_time,
            )
        except ApplicationException as error:
            logger.warning(
                "Сбой гибридного извлечения полей для источника «%s» (%s): %s",
                sanitized_item.source_name,
                sanitized_item.external_identifier,
                error,
            )
            return deterministic, f"Сбой гибридного извлечения моделью: {error}"

        merged = _merge_extracted_fields(
            deterministic=deterministic, model_extracted=llm_fields, cities=cities, venues=venues
        )
        return merged, None

    async def _find_existing_event(
        self,
        *,
        source_type: SourceType,
        source_hash: str,
        normalized_title: str,
        date_start: datetime,
        city: str,
        venue: str | None,
        deduplication_key: str,
        can_search_duplicates: bool,
        batch_events_by_identifier: Mapping[UUID, Event],
        batch_events_by_hash: Mapping[str, Event],
    ) -> tuple[Event | None, bool]:
        in_batch_by_hash = batch_events_by_hash.get(source_hash)
        if in_batch_by_hash is not None:
            return in_batch_by_hash, True

        matched_by_hash = await self._event_repository.find_by_source_hash(
            source_type=source_type, source_hash=source_hash
        )
        if matched_by_hash is not None:
            return matched_by_hash, True

        if not can_search_duplicates:
            return None, False

        for batch_candidate in batch_events_by_identifier.values():
            duplicate_check = are_events_duplicates(
                batch_candidate,
                normalized_title=normalized_title,
                date_start=date_start,
                city=city,
                venue=venue,
                deduplication_key=deduplication_key,
            )
            if duplicate_check.is_duplicate:
                return batch_candidate, False

        window_delta = timedelta(hours=_DEDUPLICATION_WINDOW_HOURS)
        nearby_page = await self._event_repository.search_events(
            search_filter=EventSearchFilter(
                city=city,
                date_from=date_start - window_delta,
                date_to=date_start + window_delta,
                page=1,
                page_size=_MAXIMUM_SEARCH_CANDIDATES,
            ),
            statuses=(EventStatus.PUBLISHED, EventStatus.DRAFT, EventStatus.CANCELED),
        )
        for existing_candidate in nearby_page.items:
            duplicate_check = are_events_duplicates(
                existing_candidate,
                normalized_title=normalized_title,
                date_start=date_start,
                city=city,
                venue=venue,
                deduplication_key=deduplication_key,
            )
            if duplicate_check.is_duplicate:
                return existing_candidate, False

        return None, False

    async def _enqueue_moderation(
        self,
        *,
        event: Event,
        raw_record: RawEventRecord,
        reasons: Sequence[str],
        has_suspicious_instructions: bool,
        matched_injection_patterns: tuple[str, ...],
        current_time: datetime,
    ) -> None:
        reason_text = " ".join(reasons) or "Карточка требует проверки модератором перед публикацией."
        await self._administration_repository.enqueue_moderation_item(
            moderation_item=ModerationItem(
                moderation_identifier=uuid4(),
                event_identifier=event.identifier,
                raw_event_identifier=raw_record.identifier,
                reason=reason_text,
                status="pending",
                has_suspicious_instructions=has_suspicious_instructions,
                matched_injection_patterns=matched_injection_patterns,
                created_at=current_time,
            )
        )

    async def _safe_check_link(self, *, target_url: str, current_time: datetime) -> LinkCheckResult:
        try:
            return await self._link_checker.check_link(
                target_url=target_url, timeout_seconds=self._link_check_timeout_seconds
            )
        except ApplicationException as error:
            logger.warning("Ошибка проверки ссылки «%s»: %s", target_url, error)
            return LinkCheckResult(
                target_url=target_url,
                is_reachable=False,
                http_status_code=None,
                final_url=None,
                error_reason=str(error),
                checked_at=current_time,
            )

    async def _safe_update_raw_status(
        self, *, raw_event_identifier: UUID, normalization_status: RawNormalizationStatus, error_message: str | None
    ) -> None:
        try:
            await self._raw_event_repository.update_normalization_status(
                raw_event_identifier=raw_event_identifier,
                normalization_status=normalization_status,
                error_message=error_message,
            )
        except ApplicationException as error:
            logger.warning("Не удалось обновить статус нормализации сырой записи %s: %s", raw_event_identifier, error)


def _resolve_current_time(current_time: datetime | None) -> datetime:
    return attach_default_timezone(current_time or datetime.now(tz=BASHKORTOSTAN_TIMEZONE))


def _compute_fetched_item_hash(item: FetchedSourceItem) -> str:
    payload: dict[str, object] = {
        "source_name": item.source_name,
        "external_identifier": item.external_identifier,
        "source_url": item.source_url,
        "raw_title": item.raw_title,
        "raw_description": item.raw_description,
        "raw_date_text": item.raw_date_text,
        "raw_price_text": item.raw_price_text,
        "raw_city": item.raw_city,
        "raw_venue": item.raw_venue,
        "raw_address": item.raw_address,
        "raw_category": item.raw_category,
        "raw_age_text": item.raw_age_text,
        "raw_language": item.raw_language,
        "organization_link": item.organization_link,
        "raw_payload": dict(item.raw_payload),
    }
    return compute_payload_hash(payload)


def _build_raw_storage_payload(item: FetchedSourceItem) -> dict[str, object]:
    payload: dict[str, object] = dict(item.raw_payload)
    payload.setdefault("raw_title", item.raw_title)
    if item.raw_description is not None:
        payload.setdefault("raw_description", item.raw_description)
    if item.raw_date_text is not None:
        payload.setdefault("raw_date_text", item.raw_date_text)
    if item.raw_price_text is not None:
        payload.setdefault("raw_price_text", item.raw_price_text)
    if item.raw_city is not None:
        payload.setdefault("raw_city", item.raw_city)
    if item.raw_venue is not None:
        payload.setdefault("raw_venue", item.raw_venue)
    if item.raw_address is not None:
        payload.setdefault("raw_address", item.raw_address)
    if item.raw_category is not None:
        payload.setdefault("raw_category", item.raw_category)
    if item.raw_age_text is not None:
        payload.setdefault("raw_age_text", item.raw_age_text)
    if item.raw_language is not None:
        payload.setdefault("raw_language", item.raw_language)
    if item.organization_link is not None:
        payload.setdefault("organization_link", item.organization_link)
    return payload


def _pick_optional_text(payload: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None or isinstance(value, bool):
            continue
        cleaned = normalize_optional_text(value if isinstance(value, str) else str(value))
        if cleaned is not None:
            return cleaned
    return None


def _extract_price_text_from_payload(payload: Mapping[str, object]) -> str | None:
    direct = _pick_optional_text(payload, "raw_price_text", "price_text", "price", "cost", "цена", "стоимость")
    if direct is not None:
        return direct
    if payload.get("is_free") is True:
        return "бесплатно"
    price_min = payload.get("price_minimum")
    price_max = payload.get("price_maximum")
    if isinstance(price_min, (int, float)) and not isinstance(price_min, bool):
        if isinstance(price_max, (int, float)) and not isinstance(price_max, bool):
            return f"от {int(price_min)} до {int(price_max)} руб."
        return f"от {int(price_min)} руб."
    return None


def _fetched_item_from_raw_record(raw_record: RawEventRecord) -> FetchedSourceItem:
    payload = raw_record.raw_payload
    raw_title = _pick_optional_text(payload, "raw_title", "title", "name", "заголовок", "название") or ""
    return FetchedSourceItem(
        source_name=raw_record.source_name,
        source_type=raw_record.source_type,
        external_identifier=raw_record.external_identifier,
        source_url=raw_record.source_url,
        raw_title=raw_title,
        raw_description=_pick_optional_text(payload, "raw_description", "description", "summary", "описание"),
        raw_date_text=_pick_optional_text(
            payload, "raw_date_text", "date_text", "date_start", "start_at", "date", "published", "дата"
        ),
        raw_price_text=_extract_price_text_from_payload(payload),
        raw_city=_pick_optional_text(payload, "raw_city", "city", "город"),
        raw_venue=_pick_optional_text(payload, "raw_venue", "venue", "place", "площадка", "место"),
        raw_address=_pick_optional_text(payload, "raw_address", "address", "адрес"),
        raw_category=_pick_optional_text(payload, "raw_category", "category", "категория"),
        raw_age_text=_pick_optional_text(payload, "raw_age_text", "age_text", "age_limit", "age", "возраст"),
        raw_language=_pick_optional_text(payload, "raw_language", "language", "язык"),
        organization_link=_pick_optional_text(payload, "organization_link", "org_link", "organizer_url"),
        raw_payload=payload,
    )


def _sanitize_fetched_item(item: FetchedSourceItem) -> _SanitizedSourceData:
    title_inspection = inspect_untrusted_text(item.raw_title, maximum_length=500)
    description_inspection = inspect_untrusted_text(item.raw_description or "", maximum_length=4000)
    city_inspection = inspect_untrusted_text(item.raw_city or "", maximum_length=150)
    venue_inspection = inspect_untrusted_text(item.raw_venue or "", maximum_length=255)
    address_inspection = inspect_untrusted_text(item.raw_address or "", maximum_length=500)
    date_inspection = inspect_untrusted_text(item.raw_date_text or "", maximum_length=300)
    price_inspection = inspect_untrusted_text(item.raw_price_text or "", maximum_length=300)

    inspections = (
        title_inspection,
        description_inspection,
        city_inspection,
        venue_inspection,
        address_inspection,
        date_inspection,
        price_inspection,
    )
    has_suspicious = any(result.has_suspicious_instructions for result in inspections)
    matched_patterns = tuple(dict.fromkeys(pattern for result in inspections for pattern in result.matched_patterns))

    sanitized_item = FetchedSourceItem(
        source_name=item.source_name,
        source_type=item.source_type,
        external_identifier=item.external_identifier,
        source_url=sanitize_control_characters(item.source_url, maximum_length=1024),
        raw_title=title_inspection.cleaned_text,
        raw_description=description_inspection.cleaned_text or None,
        raw_date_text=date_inspection.cleaned_text or None,
        raw_price_text=price_inspection.cleaned_text or None,
        raw_city=city_inspection.cleaned_text or None,
        raw_venue=venue_inspection.cleaned_text or None,
        raw_address=address_inspection.cleaned_text or None,
        raw_category=sanitize_control_characters(item.raw_category, maximum_length=120)
        if item.raw_category is not None
        else None,
        raw_age_text=sanitize_control_characters(item.raw_age_text, maximum_length=120)
        if item.raw_age_text is not None
        else None,
        raw_language=sanitize_control_characters(item.raw_language, maximum_length=64)
        if item.raw_language is not None
        else None,
        organization_link=sanitize_control_characters(item.organization_link, maximum_length=1024)
        if item.organization_link is not None
        else None,
        raw_payload=item.raw_payload,
    )
    return _SanitizedSourceData(
        item=sanitized_item,
        cleaned_description=description_inspection.cleaned_text,
        has_suspicious_instructions=has_suspicious,
        matched_injection_patterns=matched_patterns,
    )


def _snap_age_limit(value: int | None) -> int | None:
    if value is None:
        return None
    if value <= 0:
        return 0
    if value <= 6:
        return 6
    if value <= 12:
        return 12
    if value <= 16:
        return 16
    return 18


def _merge_extracted_fields(
    *,
    deterministic: ExtractedEventFields,
    model_extracted: ExtractedEventFields,
    cities: RealCollection[CityReference],
    venues: RealCollection[VenueReference],
) -> ExtractedEventFields:
    merged_title = deterministic.title or normalize_optional_text(
        sanitize_control_characters(model_extracted.title, maximum_length=500)
        if model_extracted.title is not None
        else None
    )
    merged_category = (
        deterministic.category
        if deterministic.category is not None and deterministic.category is not EventCategory.OTHER
        else (model_extracted.category or deterministic.category)
    )

    merged_date_start = deterministic.date_start
    if merged_date_start is None and model_extracted.date_start is not None:
        merged_date_start = attach_default_timezone(model_extracted.date_start)

    merged_date_end = deterministic.date_end
    if merged_date_end is None and model_extracted.date_end is not None:
        candidate_end = attach_default_timezone(model_extracted.date_end)
        if merged_date_start is None or candidate_end >= merged_date_start:
            merged_date_end = candidate_end

    merged_duration = deterministic.duration_minutes
    if (
        merged_duration is None
        and model_extracted.duration_minutes is not None
        and model_extracted.duration_minutes > 0
    ):
        merged_duration = model_extracted.duration_minutes

    merged_city = deterministic.city
    if merged_city is None and model_extracted.city is not None:
        cleaned_model_city = normalize_optional_text(
            sanitize_control_characters(model_extracted.city, maximum_length=150)
        )
        merged_city = normalize_city(cleaned_model_city, cities=cities)

    merged_venue = deterministic.venue
    merged_address = deterministic.address
    if merged_venue is None and model_extracted.venue is not None:
        cleaned_model_venue = normalize_optional_text(
            sanitize_control_characters(model_extracted.venue, maximum_length=255)
        )
        venue_match = normalize_venue(cleaned_model_venue, venues=venues, city=merged_city)
        if isinstance(venue_match, VenueReference):
            merged_venue = venue_match.name
            merged_address = merged_address or venue_match.address
            merged_city = merged_city or venue_match.city
        else:
            merged_venue = venue_match

    if merged_address is None and model_extracted.address is not None:
        merged_address = normalize_optional_text(
            sanitize_control_characters(model_extracted.address, maximum_length=500)
        )

    has_det_pricing = (
        deterministic.price_minimum is not None
        or deterministic.price_maximum is not None
        or deterministic.is_free is not None
    )
    if has_det_pricing:
        merged_price_min = deterministic.price_minimum
        merged_price_max = deterministic.price_maximum
        merged_is_free = deterministic.is_free
    else:
        merged_price_min = model_extracted.price_minimum
        merged_price_max = model_extracted.price_maximum
        merged_is_free = model_extracted.is_free

    merged_age = (
        deterministic.age_limit if deterministic.age_limit is not None else _snap_age_limit(model_extracted.age_limit)
    )
    merged_language = deterministic.language or _normalize_language_value(model_extracted.language)

    merged_tags_list = list(deterministic.tags)
    for raw_tag in model_extracted.tags:
        cleaned_tag = normalize_optional_text(sanitize_control_characters(raw_tag, maximum_length=64))
        if cleaned_tag is not None:
            lowered_tag = cleaned_tag.lower()
            if lowered_tag not in merged_tags_list:
                merged_tags_list.append(lowered_tag)

    merged_confidence = round(min(max(max(deterministic.confidence, model_extracted.confidence), 0.0), 1.0), 4)

    return ExtractedEventFields(
        title=merged_title,
        category=merged_category,
        date_start=merged_date_start,
        date_end=merged_date_end,
        duration_minutes=merged_duration,
        city=merged_city,
        venue=merged_venue,
        address=merged_address,
        price_minimum=merged_price_min,
        price_maximum=merged_price_max,
        is_free=merged_is_free,
        age_limit=merged_age,
        language=merged_language,
        tags=tuple(merged_tags_list),
        confidence=merged_confidence,
    )


def _is_event_canceled(*, raw_payload: Mapping[str, object], raw_title: str, extracted_title: str | None) -> bool:
    for status_key in ("status", "статус", "event_status"):
        status_value = raw_payload.get(status_key)
        if isinstance(status_value, str) and status_value.strip().lower() in _CANCELED_STATUS_VALUES:
            return True

    for bool_key in ("is_canceled", "is_cancelled", "canceled", "cancelled"):
        if raw_payload.get(bool_key) is True:
            return True

    for candidate_title in (raw_title, extracted_title):
        if candidate_title and _CANCELED_TITLE_PATTERN.search(candidate_title) is not None:
            return True

    return False


def _has_valid_title(title: str | None) -> bool:
    cleaned = normalize_optional_text(title)
    if cleaned is None:
        return False
    return bool(normalize_title(cleaned))


def _is_event_in_past(*, date_start: datetime, date_end: datetime | None, current_time: datetime) -> bool:
    if date_end is not None:
        return attach_default_timezone(date_end) < current_time
    if date_start.hour == 0 and date_start.minute == 0:
        return attach_default_timezone(date_start).date() < current_time.date()
    return attach_default_timezone(date_start) < current_time


def _collect_moderation_reasons(
    *,
    is_canceled: bool,
    has_suspicious_instructions: bool,
    link_result: LinkCheckResult,
    is_past_event: bool,
    has_real_title: bool,
    has_real_city: bool,
    has_real_date: bool,
    confidence: float,
    confidence_threshold: float,
    extractor_error_reason: str | None,
) -> tuple[str, ...]:
    if is_canceled and not has_suspicious_instructions:
        return ()

    reasons: list[str] = []
    if has_suspicious_instructions:
        reasons.append("Обнаружены подозрительные инструкции в тексте карточки (риск инъекции).")
    if not is_canceled:
        if not link_result.is_reachable:
            reasons.append(f"Ссылка на первоисточник недоступна ({_format_link_failure_detail(link_result)}).")
        if is_past_event:
            reasons.append("Дата проведения мероприятия находится в прошлом относительно времени сбора.")
        if not has_real_title:
            reasons.append("Отсутствует корректное название мероприятия.")
        if not has_real_city:
            reasons.append("Не удалось определить город проведения (назначено «Уточняется»).")
        if not has_real_date:
            reasons.append("Не удалось распознать дату начала (для черновика назначена опорная дата).")
        if extractor_error_reason is not None:
            reasons.append(extractor_error_reason)
        if confidence < confidence_threshold:
            reasons.append(f"Уверенность извлечения ({confidence:.2f}) ниже порога ({confidence_threshold:.2f}).")
    return tuple(reasons)


def _format_link_failure_detail(link_result: LinkCheckResult) -> str:
    if link_result.error_reason:
        return link_result.error_reason
    if link_result.http_status_code is not None:
        return f"HTTP {link_result.http_status_code}"
    return "ресурс не отвечает"


def _safe_resolve_duration(
    *, date_start: datetime, date_end: datetime | None, explicit_duration: int | None
) -> int | None:
    if explicit_duration is not None and explicit_duration <= 0:
        explicit_duration = None
    return resolve_duration_minutes(
        date_start=date_start, date_end=date_end, explicit_duration_minutes=explicit_duration
    )


def _resolve_coordinates(
    *,
    effective_venue: str,
    effective_city: str,
    venues: RealCollection[VenueReference],
    raw_payload: Mapping[str, object],
) -> tuple[float | None, float | None]:
    venue_match = normalize_venue(effective_venue, venues=venues, city=effective_city)
    if isinstance(venue_match, VenueReference) and venue_match.coordinates is not None:
        return venue_match.coordinates.latitude, venue_match.coordinates.longitude

    lat = _parse_coordinate(raw_payload.get("latitude"), minimum=-90.0, maximum=90.0)
    lon = _parse_coordinate(raw_payload.get("longitude"), minimum=-180.0, maximum=180.0)
    if lat is not None and lon is not None:
        return lat, lon
    return None, None


def _parse_coordinate(value: object, *, minimum: float, maximum: float) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        cleaned = normalize_optional_text(value)
        if cleaned is None:
            return None
        try:
            numeric = float(cleaned.replace(",", "."))
        except ValueError:
            return None
    else:
        return None
    if minimum <= numeric <= maximum:
        return numeric
    return None


def _normalize_pricing_constraints(
    *, price_minimum: int | None, price_maximum: int | None, is_free: bool | None
) -> tuple[int | None, int | None, bool]:
    valid_min = price_minimum if price_minimum is not None and price_minimum >= 0 else None
    valid_max = price_maximum if price_maximum is not None and price_maximum >= 0 else None
    if valid_min is not None and valid_max is not None and valid_min > valid_max:
        valid_min, valid_max = valid_max, valid_min

    effective_free = (
        bool(is_free) if is_free is not None else (valid_min == 0 and (valid_max is None or valid_max == 0))
    )
    if effective_free and ((valid_min is not None and valid_min != 0) or (valid_max is not None and valid_max != 0)):
        effective_free = False
    if effective_free and valid_min is None and valid_max is None:
        valid_min = 0
        valid_max = 0

    return valid_min, valid_max, effective_free


def _normalize_language_value(raw_language: str | None) -> str | None:
    cleaned = normalize_optional_text(raw_language)
    if cleaned is None:
        return None
    return cleaned.lower()[:64]


def _normalize_organization_link(raw_link: str | None) -> str | None:
    cleaned = normalize_optional_text(raw_link)
    if cleaned is None:
        return None
    return cleaned[:1024]


def _resolve_merged_status(
    *, existing_status: EventStatus, is_canceled: bool, has_suspicious_instructions: bool
) -> EventStatus:
    if is_canceled or existing_status is EventStatus.CANCELED:
        return EventStatus.CANCELED
    if has_suspicious_instructions:
        return EventStatus.DRAFT
    return existing_status


def _build_reprocessed_same_source_event(
    *,
    existing_event: Event,
    source_reference: EventSourceReference,
    fields: ExtractedEventFields,
    normalized_title: str,
    description: str,
    latitude: float | None,
    longitude: float | None,
    organization_link: str | None,
    deduplication_key: str,
    status: EventStatus,
    updated_at: datetime,
) -> Event:
    preserved_sources: list[EventSourceReference] = [source_reference]
    for source_item in existing_event.sources:
        if (
            source_item.source_url == source_reference.source_url
            and source_item.source_type == source_reference.source_type
        ):
            continue
        if (source_item.source_type, source_item.source_hash) == (
            source_reference.source_type,
            source_reference.source_hash,
        ):
            continue
        preserved_sources.append(source_item)

    merged_tags = list(existing_event.tags)
    for tag in fields.tags:
        if tag not in merged_tags:
            merged_tags.append(tag)

    return Event(
        identifier=existing_event.identifier,
        title=fields.title or existing_event.title,
        normalized_title=normalized_title,
        category=fields.category or existing_event.category,
        tags=tuple(merged_tags),
        date_start=fields.date_start or existing_event.date_start,
        date_end=fields.date_end,
        duration_minutes=fields.duration_minutes,
        city=fields.city or existing_event.city,
        venue=fields.venue or existing_event.venue,
        address=fields.address if fields.address is not None else existing_event.address,
        latitude=latitude if latitude is not None else existing_event.latitude,
        longitude=longitude if longitude is not None else existing_event.longitude,
        description=description,
        price_minimum=fields.price_minimum,
        price_maximum=fields.price_maximum,
        is_free=bool(fields.is_free),
        age_limit=fields.age_limit if fields.age_limit is not None else existing_event.age_limit,
        language=fields.language,
        organization_link=organization_link or existing_event.organization_link,
        source_url=source_reference.source_url,
        source_type=source_reference.source_type,
        source_hash=source_reference.source_hash,
        verified_at=source_reference.verified_at or existing_event.verified_at,
        confidence=fields.confidence,
        status=status,
        deduplication_key=deduplication_key,
        created_at=existing_event.created_at,
        updated_at=updated_at,
        sources=tuple(preserved_sources),
    )

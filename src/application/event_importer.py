"""Прикладной сервис идемпотентного импорта мероприятий из файлов CSV и JSON."""

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from src.application._helpers import attach_default_timezone, resolve_duration_minutes, validate_http_url
from src.core.exceptions import DomainException
from src.core.types import EventStatus, RealCollection, SourceType
from src.domain.entities import (
    AuditLogEntry,
    CityReference,
    Event,
    EventSearchFilter,
    EventSourceReference,
    ExtractedEventFields,
    ImportReport,
    ImportRowFailure,
    ModerationItem,
    VenueReference,
)
from src.domain.exceptions import EventNotFoundError, InvalidEventDataError, UnsupportedSourceFormatError
from src.domain.ports import AdministrationRepositoryPort, EventRepositoryPort
from src.domain.services import (
    BASHKORTOSTAN_TIMEZONE,
    are_events_duplicates,
    compute_deduplication_key,
    compute_payload_hash,
    extract_event_tags,
    inspect_untrusted_text,
    merge_event_sources,
    normalize_city,
    normalize_optional_text,
    normalize_title,
    normalize_venue,
    parse_age_limit,
    parse_category,
    parse_event_datetime,
    parse_language,
    parse_price_range,
)

__all__ = ["EventImportService"]

type ImportFormat = Literal["csv", "json"]

_ALLOWED_AGE_LIMITS = frozenset({0, 6, 12, 16, 18})
_DEDUPLICATION_WINDOW_HOURS = 24
_MAXIMUM_SEARCH_CANDIDATES = 100

_FIELD_ALIASES: dict[str, str] = {
    "identifier": "identifier",
    "id": "identifier",
    "идентификатор": "identifier",
    "external_identifier": "external_identifier",
    "external_id": "external_identifier",
    "внешний_идентификатор": "external_identifier",
    "title": "title",
    "name": "title",
    "название": "title",
    "заголовок": "title",
    "category": "category",
    "категория": "category",
    "date_start": "date_start",
    "start_at": "date_start",
    "date": "date_start",
    "дата": "date_start",
    "дата_начала": "date_start",
    "date_end": "date_end",
    "end_at": "date_end",
    "дата_окончания": "date_end",
    "duration_minutes": "duration_minutes",
    "duration": "duration_minutes",
    "длительность": "duration_minutes",
    "city": "city",
    "город": "city",
    "venue": "venue",
    "place": "venue",
    "площадка": "venue",
    "место": "venue",
    "address": "address",
    "адрес": "address",
    "description": "description",
    "описание": "description",
    "price_minimum": "price_minimum",
    "price_min": "price_minimum",
    "цена_от": "price_minimum",
    "price_maximum": "price_maximum",
    "price_max": "price_maximum",
    "цена_до": "price_maximum",
    "price": "price",
    "цена": "price",
    "стоимость": "price",
    "is_free": "is_free",
    "free": "is_free",
    "бесплатно": "is_free",
    "age_limit": "age_limit",
    "age": "age_limit",
    "возраст": "age_limit",
    "возрастное_ограничение": "age_limit",
    "language": "language",
    "язык": "language",
    "tags": "tags",
    "теги": "tags",
    "метки": "tags",
    "organization_link": "organization_link",
    "organizer_url": "organization_link",
    "ссылка_организатора": "organization_link",
    "source_url": "source_url",
    "url": "source_url",
    "ссылка": "source_url",
    "source_name": "source_name",
    "source": "source_name",
    "источник": "source_name",
    "status": "status",
    "статус": "status",
    "latitude": "latitude",
    "широта": "latitude",
    "longitude": "longitude",
    "долгота": "longitude",
}

_BOOLEAN_TRUE_VALUES = frozenset({"1", "true", "yes", "да", "бесплатно", "y"})
_BOOLEAN_FALSE_VALUES = frozenset({"0", "false", "no", "нет", "платно", "n"})

_STATUS_MAP: dict[str, EventStatus] = {
    "published": EventStatus.PUBLISHED,
    "опубликовано": EventStatus.PUBLISHED,
    "draft": EventStatus.DRAFT,
    "черновик": EventStatus.DRAFT,
    "canceled": EventStatus.CANCELED,
    "cancelled": EventStatus.CANCELED,
    "отменено": EventStatus.CANCELED,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class _ParsedImportRecord:
    """Промежуточная проверенная строка импорта перед записью или слиянием."""

    row_number: int
    external_identifier: str | None
    explicit_identifier: UUID | None
    candidate_event: Event
    has_suspicious_instructions: bool
    matched_injection_patterns: tuple[str, ...]


class EventImportService:
    """Прикладной сервис пакетного идемпотентного импорта мероприятий из CSV и JSON."""

    def __init__(
        self,
        *,
        event_repository: EventRepositoryPort,
        administration_repository: AdministrationRepositoryPort | None = None,
    ) -> None:
        """Инициализирует сервис импорта с репозиториями каталога и администрирования.

        Arguments:
            event_repository: Реализация порта каталога мероприятий.
            administration_repository: Необязательный порт для постановки в модерацию и записи аудита.
        """
        self._event_repository = event_repository
        self._administration_repository = administration_repository

    async def import_events(
        self,
        *,
        raw_content: str | bytes,
        file_format: ImportFormat | None = None,
        filename: str | None = None,
        actor: str = "importer",
        source_name_override: str | None = None,
        default_source_type: SourceType = SourceType.IMPORT_FILE,
        reference_datetime: datetime | None = None,
    ) -> ImportReport:
        """Выполняет идемпотентный импорт карточек из CSV или JSON с дедупликацией и отчётом.

        Каждая строка валидируется независимо: некорректные записи попадают в `failures`
        без прерывания обработки остального файла. Записи с подозрительными инструкциями
        принудительно переводятся в статус `DRAFT` и направляются в очередь модерации.

        Arguments:
            raw_content: Содержимое импортируемого файла в виде строки или байтов UTF-8.
            file_format: Явно заданный формат (`csv` или `json`).
            filename: Имя файла для автоматического определения формата и журнала изменений.
            actor: Инициатор операции импорта для аудита.
            source_name_override: Общее название источника для записей без колонки источника.
            default_source_type: Тип источника для импортированных записей.
            reference_datetime: Опорная метка времени для относительных дат и штампов обновления.

        Returns:
            Сводный отчёт импорта `ImportReport`.

        Raises:
            UnsupportedSourceFormatError: Если формат не поддерживается, кодировка повреждена
                или файл синтаксически некорректен.
        """
        current_time = attach_default_timezone(reference_datetime or datetime.now(tz=BASHKORTOSTAN_TIMEZONE))

        resolved_format = _detect_import_format(raw_content=raw_content, file_format=file_format, filename=filename)
        decoded_text = _decode_import_text(raw_content, filename=filename, file_format=resolved_format)

        raw_rows: list[tuple[int, object]]
        if resolved_format == "json":
            raw_rows = _parse_json_records(decoded_text, filename=filename)
        else:
            raw_rows = _parse_csv_records(decoded_text, filename=filename)

        if not raw_rows:
            return ImportReport(
                rows_total=0, rows_added=0, rows_updated=0, rows_skipped_unchanged=0, rows_rejected=0, failures=()
            )

        cities = await self._event_repository.list_cities()
        venues = await self._event_repository.list_venues()

        default_source_name = normalize_optional_text(source_name_override) or (
            f"Импорт: {filename}" if filename else "Файловый импорт"
        )

        rows_added = 0
        rows_updated = 0
        rows_skipped_unchanged = 0
        failures: list[ImportRowFailure] = []
        batch_events_by_identifier: dict[UUID, Event] = {}
        batch_events_by_hash: dict[str, Event] = {}

        for row_number, raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                failures.append(
                    ImportRowFailure(
                        row_number=row_number,
                        external_identifier=None,
                        reason="Запись должна быть объектом (словарем полей).",
                    )
                )
                continue

            if raw_row.get(None):
                failures.append(
                    ImportRowFailure(
                        row_number=row_number,
                        external_identifier=_extract_row_identifier_hint(raw_row),
                        reason="Количество столбцов в строке превышает заголовок CSV.",
                    )
                )
                continue

            try:
                parsed_record = _parse_single_row(
                    row_number=row_number,
                    raw_row=raw_row,
                    cities=cities,
                    venues=venues,
                    default_source_name=default_source_name,
                    default_source_type=default_source_type,
                    reference_datetime=current_time,
                )
            except (DomainException, ValueError) as error:
                failures.append(
                    ImportRowFailure(
                        row_number=row_number,
                        external_identifier=_extract_row_identifier_hint(raw_row),
                        reason=str(error),
                    )
                )
                continue

            outcome, saved_event = await self._persist_or_merge_record(
                parsed_record=parsed_record,
                current_time=current_time,
                batch_events_by_identifier=batch_events_by_identifier,
                batch_events_by_hash=batch_events_by_hash,
            )

            if saved_event is not None:
                batch_events_by_identifier[saved_event.identifier] = saved_event
                batch_events_by_hash[parsed_record.candidate_event.source_hash] = saved_event
                for source_reference in saved_event.sources:
                    batch_events_by_hash[source_reference.source_hash] = saved_event

            if outcome == "added":
                rows_added += 1
            elif outcome == "updated":
                rows_updated += 1
            else:
                rows_skipped_unchanged += 1

        report = ImportReport(
            rows_total=len(raw_rows),
            rows_added=rows_added,
            rows_updated=rows_updated,
            rows_skipped_unchanged=rows_skipped_unchanged,
            rows_rejected=len(failures),
            failures=tuple(failures),
        )

        if self._administration_repository is not None and (rows_added > 0 or rows_updated > 0):
            await self._administration_repository.append_audit_log(
                entry=AuditLogEntry(
                    entry_identifier=uuid4(),
                    actor=normalize_optional_text(actor) or "importer",
                    action="import",
                    entity_type="event_catalog",
                    entity_identifier=filename or resolved_format,
                    changes={
                        "format": resolved_format,
                        "rows_total": report.rows_total,
                        "rows_added": report.rows_added,
                        "rows_updated": report.rows_updated,
                        "rows_skipped_unchanged": report.rows_skipped_unchanged,
                        "rows_rejected": report.rows_rejected,
                    },
                    created_at=current_time,
                )
            )

        return report

    async def _persist_or_merge_record(
        self,
        *,
        parsed_record: _ParsedImportRecord,
        current_time: datetime,
        batch_events_by_identifier: Mapping[UUID, Event],
        batch_events_by_hash: Mapping[str, Event],
    ) -> tuple[Literal["added", "updated", "skipped"], Event | None]:
        candidate = parsed_record.candidate_event

        existing_event = await self._find_existing_event(
            parsed_record=parsed_record,
            batch_events_by_identifier=batch_events_by_identifier,
            batch_events_by_hash=batch_events_by_hash,
        )

        if existing_event is None:
            saved_event = await self._event_repository.save_event(event=candidate)
            await self._enqueue_injection_moderation_if_needed(
                parsed_record=parsed_record, saved_event=saved_event, current_time=current_time
            )
            return "added", saved_event

        if _is_unchanged_import_match(existing_event=existing_event, candidate=candidate):
            return "skipped", existing_event

        is_same_source = existing_event.source_url == candidate.source_url or (
            parsed_record.explicit_identifier is not None
            and parsed_record.explicit_identifier == existing_event.identifier
        )

        if is_same_source:
            updated_event = _build_updated_same_source_event(
                existing_event=existing_event,
                candidate=candidate,
                updated_at=current_time,
                force_draft=parsed_record.has_suspicious_instructions,
            )
        else:
            updated_fields = ExtractedEventFields(
                title=candidate.title,
                category=candidate.category,
                date_start=candidate.date_start,
                date_end=candidate.date_end,
                duration_minutes=candidate.duration_minutes,
                city=candidate.city,
                venue=candidate.venue,
                address=candidate.address,
                price_minimum=candidate.price_minimum,
                price_maximum=candidate.price_maximum,
                is_free=candidate.is_free,
                age_limit=candidate.age_limit,
                language=candidate.language,
                tags=candidate.tags,
                confidence=candidate.confidence,
            )
            target_status = EventStatus.DRAFT if parsed_record.has_suspicious_instructions else existing_event.status
            updated_event = merge_event_sources(
                existing_event,
                new_source=candidate.sources[0],
                updated_fields=updated_fields,
                updated_at=current_time,
                new_status=target_status,
            )

        saved_event = await self._event_repository.save_event(event=updated_event)
        await self._enqueue_injection_moderation_if_needed(
            parsed_record=parsed_record, saved_event=saved_event, current_time=current_time
        )
        return "updated", saved_event

    async def _find_existing_event(
        self,
        *,
        parsed_record: _ParsedImportRecord,
        batch_events_by_identifier: Mapping[UUID, Event],
        batch_events_by_hash: Mapping[str, Event],
    ) -> Event | None:
        candidate = parsed_record.candidate_event

        in_batch_by_hash = batch_events_by_hash.get(candidate.source_hash)
        if in_batch_by_hash is not None:
            return in_batch_by_hash

        if parsed_record.explicit_identifier is not None:
            in_batch_by_identifier = batch_events_by_identifier.get(parsed_record.explicit_identifier)
            if in_batch_by_identifier is not None:
                return in_batch_by_identifier
            try:
                return await self._event_repository.get_event_by_identifier(
                    event_identifier=parsed_record.explicit_identifier, include_non_published=True
                )
            except EventNotFoundError:
                pass

        matched_by_hash = await self._event_repository.find_by_source_hash(
            source_type=candidate.source_type, source_hash=candidate.source_hash
        )
        if matched_by_hash is not None:
            return matched_by_hash

        for batch_candidate in batch_events_by_identifier.values():
            duplicate_check = are_events_duplicates(
                batch_candidate,
                normalized_title=candidate.normalized_title,
                date_start=candidate.date_start,
                city=candidate.city,
                venue=candidate.venue,
                deduplication_key=candidate.deduplication_key,
            )
            if duplicate_check.is_duplicate:
                return batch_candidate

        window_delta = timedelta(hours=_DEDUPLICATION_WINDOW_HOURS)
        nearby_page = await self._event_repository.search_events(
            search_filter=EventSearchFilter(
                city=candidate.city,
                date_from=candidate.date_start - window_delta,
                date_to=candidate.date_start + window_delta,
                page=1,
                page_size=_MAXIMUM_SEARCH_CANDIDATES,
            ),
            statuses=(EventStatus.PUBLISHED, EventStatus.DRAFT, EventStatus.CANCELED),
        )
        for existing_candidate in nearby_page.items:
            duplicate_check = are_events_duplicates(
                existing_candidate,
                normalized_title=candidate.normalized_title,
                date_start=candidate.date_start,
                city=candidate.city,
                venue=candidate.venue,
                deduplication_key=candidate.deduplication_key,
            )
            if duplicate_check.is_duplicate:
                return existing_candidate

        return None

    async def _enqueue_injection_moderation_if_needed(
        self, *, parsed_record: _ParsedImportRecord, saved_event: Event, current_time: datetime
    ) -> None:
        if not parsed_record.has_suspicious_instructions or self._administration_repository is None:
            return

        await self._administration_repository.enqueue_moderation_item(
            moderation_item=ModerationItem(
                moderation_identifier=uuid4(),
                event_identifier=saved_event.identifier,
                reason="Импортируемая карточка содержит подозрительные инструкции и скрыта до проверки.",
                status="pending",
                has_suspicious_instructions=True,
                matched_injection_patterns=parsed_record.matched_injection_patterns,
                created_at=current_time,
            )
        )


def _detect_import_format(
    *, raw_content: str | bytes, file_format: ImportFormat | None, filename: str | None
) -> ImportFormat:
    if file_format is not None:
        lowered_format = file_format.lower()
        if lowered_format == "csv":
            return "csv"
        if lowered_format == "json":
            return "json"
        raise UnsupportedSourceFormatError(
            f"Формат импорта «{file_format}» не поддерживается.",
            format_name=file_format,
            source_name=filename,
            reason="Ожидаются файлы CSV или JSON",
        )

    if filename is not None:
        lowered_name = filename.strip().lower()
        if lowered_name.endswith(".json"):
            return "json"
        if lowered_name.endswith(".csv"):
            return "csv"
        if "." in lowered_name:
            extension = lowered_name.rsplit(".", maxsplit=1)[-1]
            raise UnsupportedSourceFormatError(
                f"Расширение файла «.{extension}» не поддерживается для импорта.",
                format_name=extension,
                source_name=filename,
                reason="Допустимы только файлы .csv и .json",
            )

    if isinstance(raw_content, bytes):
        if b"\x00" in raw_content:
            raise UnsupportedSourceFormatError(
                "Бинарные файлы не поддерживаются для импорта афиши.",
                format_name="binary",
                source_name=filename,
                reason="Обнаружены нулевые байты в содержимом",
            )
        preview = raw_content[:256].decode("utf-8-sig", errors="ignore").lstrip()
    else:
        if "\x00" in raw_content:
            raise UnsupportedSourceFormatError(
                "Некорректные нулевые символы в содержимом импорта.",
                format_name="binary",
                source_name=filename,
                reason="Файл содержит бинарные последовательности",
            )
        preview = raw_content.lstrip()

    if preview.startswith("<"):
        raise UnsupportedSourceFormatError(
            "Разметка HTML/XML не поддерживается в пакетном импорте.",
            format_name="html_or_xml",
            source_name=filename,
            reason="Поддерживаются только CSV и JSON",
        )
    if preview.startswith("{") or preview.startswith("["):
        return "json"
    return "csv"


def _decode_import_text(raw_content: str | bytes, *, filename: str | None, file_format: ImportFormat) -> str:
    if isinstance(raw_content, bytes):
        if b"\x00" in raw_content:
            raise UnsupportedSourceFormatError(
                "Бинарный файл не является корректным текстовым документом UTF-8.",
                format_name=file_format,
                source_name=filename,
                reason="Обнаружены бинарные данные",
            )
        try:
            text = raw_content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise UnsupportedSourceFormatError(
                "Не удалось декодировать файл импорта в кодировке UTF-8.",
                format_name=file_format,
                source_name=filename,
                reason=str(error),
            ) from error
    else:
        if "\x00" in raw_content:
            raise UnsupportedSourceFormatError(
                "Содержимое импорта содержит недопустимые нулевые символы.",
                format_name=file_format,
                source_name=filename,
                reason="Обнаружены бинарные данные",
            )
        text = raw_content.removeprefix("\ufeff")

    if not text.strip():
        raise UnsupportedSourceFormatError(
            "Файл импорта пуст и не содержит данных.",
            format_name=file_format,
            source_name=filename,
            reason="Пустой файл",
        )
    return text


def _parse_json_records(decoded_text: str, *, filename: str | None) -> list[tuple[int, object]]:
    try:
        payload: object = json.loads(decoded_text)
    except json.JSONDecodeError as error:
        raise UnsupportedSourceFormatError(
            "Синтаксическая ошибка в JSON-файле импорта.", format_name="json", source_name=filename, reason=error.msg
        ) from error

    items: object
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, Mapping):
        if "events" in payload:
            items = payload["events"]
        elif "items" in payload:
            items = payload["items"]
        else:
            raise UnsupportedSourceFormatError(
                "JSON-объект импорта должен содержать список `events` или `items`.",
                format_name="json",
                source_name=filename,
                reason="Отсутствует массив мероприятий",
            )
    else:
        raise UnsupportedSourceFormatError(
            "Корень JSON-файла должен быть массивом мероприятий или объектом со списком `events`.",
            format_name="json",
            source_name=filename,
            reason="Некорректная корневая структура JSON",
        )

    if not isinstance(items, list):
        raise UnsupportedSourceFormatError(
            "Поле со списком мероприятий в JSON должно быть массивом.",
            format_name="json",
            source_name=filename,
            reason="Ожидался массив записей",
        )

    return [(index, item) for index, item in enumerate(items, start=1)]


def _detect_csv_delimiter(sample_text: str) -> str:
    first_lines = "\n".join(sample_text.splitlines()[:5])
    try:
        dialect = csv.Sniffer().sniff(first_lines, delimiters=";,\t")
        return dialect.delimiter
    except csv.Error:
        first_line = first_lines.splitlines()[0] if first_lines else ""
        if first_line.count(";") >= first_line.count(","):
            return ";" if ";" in first_line else ","
        return ","


def _parse_csv_records(decoded_text: str, *, filename: str | None) -> list[tuple[int, object]]:
    delimiter = _detect_csv_delimiter(decoded_text)
    try:
        reader = csv.DictReader(io.StringIO(decoded_text, newline=""), delimiter=delimiter, restkey=None, restval=None)
    except csv.Error as error:
        raise UnsupportedSourceFormatError(
            "Не удалось прочитать структуру CSV-файла.", format_name="csv", source_name=filename, reason=str(error)
        ) from error

    if not reader.fieldnames:
        raise UnsupportedSourceFormatError(
            "В CSV-файле отсутствует строка заголовков.",
            format_name="csv",
            source_name=filename,
            reason="Отсутствует заголовок столбцов",
        )

    normalized_headers = [header.strip().lower() if isinstance(header, str) else "" for header in reader.fieldnames]
    if not any(normalized_headers) or any(header == "" for header in normalized_headers):
        raise UnsupportedSourceFormatError(
            "Заголовки столбцов CSV содержат пустые имена.",
            format_name="csv",
            source_name=filename,
            reason="Некорректный заголовок CSV",
        )

    reader.fieldnames = normalized_headers
    records: list[tuple[int, object]] = []
    try:
        for row_number, row in enumerate(reader, start=1):
            if _is_completely_empty_csv_row(row):
                continue
            records.append((row_number, row))
    except csv.Error as error:
        raise UnsupportedSourceFormatError(
            "Синтаксическая ошибка при чтении строк CSV-файла.",
            format_name="csv",
            source_name=filename,
            reason=str(error),
        ) from error

    return records


def _is_completely_empty_csv_row(row: Mapping[object, object]) -> bool:
    for key, value in row.items():
        if key is None:
            return False
        if isinstance(value, str):
            if value.strip():
                return False
        elif value is not None:
            return False
    return True


def _canonicalize_row_fields(raw_row: Mapping[object, object]) -> dict[str, object]:
    canonical: dict[str, object] = {}
    for raw_key, raw_value in raw_row.items():
        if not isinstance(raw_key, str):
            continue
        cleaned_key = raw_key.strip().lower().replace(" ", "_")
        target_field = _FIELD_ALIASES.get(cleaned_key, cleaned_key)
        if target_field not in canonical or canonical[target_field] in (None, ""):
            canonical[target_field] = raw_value
    return canonical


def _extract_row_identifier_hint(raw_row: Mapping[object, object]) -> str | None:
    canonical = _canonicalize_row_fields(raw_row)
    for field_name in ("external_identifier", "identifier", "title"):
        value = canonical.get(field_name)
        if value is not None:
            cleaned = normalize_optional_text(str(value))
            if cleaned is not None:
                return cleaned
    return None


def _parse_single_row(
    *,
    row_number: int,
    raw_row: Mapping[object, object],
    cities: RealCollection[CityReference],
    venues: RealCollection[VenueReference],
    default_source_name: str,
    default_source_type: SourceType,
    reference_datetime: datetime,
) -> _ParsedImportRecord:
    fields = _canonicalize_row_fields(raw_row)

    external_identifier = _as_optional_string(fields.get("external_identifier")) or _as_optional_string(
        fields.get("identifier")
    )
    explicit_identifier = _parse_optional_uuid(fields.get("identifier"))

    raw_title = _as_optional_string(fields.get("title"))
    if raw_title is None:
        raise InvalidEventDataError("Отсутствует обязательное название мероприятия.", field_name="title")

    raw_description = _as_optional_string(fields.get("description")) or ""
    raw_city = _as_optional_string(fields.get("city"))
    if raw_city is None:
        raise InvalidEventDataError("Отсутствует обязательный город проведения.", field_name="city")

    raw_venue = _as_optional_string(fields.get("venue"))
    if raw_venue is None:
        raise InvalidEventDataError("Отсутствует обязательная площадка проведения.", field_name="venue")

    raw_address = _as_optional_string(fields.get("address"))
    title_inspection = inspect_untrusted_text(raw_title, maximum_length=500)
    description_inspection = inspect_untrusted_text(raw_description, maximum_length=4000)
    venue_inspection = inspect_untrusted_text(raw_venue, maximum_length=300)
    address_inspection = inspect_untrusted_text(raw_address or "", maximum_length=500)

    cleaned_title = normalize_optional_text(title_inspection.cleaned_text)
    if cleaned_title is None:
        raise InvalidEventDataError("Название мероприятия пустое после очистки.", field_name="title")

    cleaned_description = description_inspection.cleaned_text
    cleaned_venue_raw = normalize_optional_text(venue_inspection.cleaned_text)
    if cleaned_venue_raw is None:
        raise InvalidEventDataError("Название площадки пустое после очистки.", field_name="venue")

    matched_patterns = tuple(
        dict.fromkeys(
            [
                *title_inspection.matched_patterns,
                *description_inspection.matched_patterns,
                *venue_inspection.matched_patterns,
                *address_inspection.matched_patterns,
            ]
        )
    )
    has_suspicious_instructions = bool(
        title_inspection.has_suspicious_instructions
        or description_inspection.has_suspicious_instructions
        or venue_inspection.has_suspicious_instructions
        or address_inspection.has_suspicious_instructions
    )

    resolved_city = normalize_city(raw_city, cities=cities) or raw_city
    matched_venue = normalize_venue(cleaned_venue_raw, venues=venues, city=resolved_city)
    resolved_address: str | None
    if isinstance(matched_venue, VenueReference):
        resolved_venue = matched_venue.name
        resolved_address = normalize_optional_text(address_inspection.cleaned_text) or matched_venue.address
        default_latitude = matched_venue.coordinates.latitude if matched_venue.coordinates else None
        default_longitude = matched_venue.coordinates.longitude if matched_venue.coordinates else None
    else:
        resolved_venue = cleaned_venue_raw
        resolved_address = normalize_optional_text(address_inspection.cleaned_text)
        default_latitude = None
        default_longitude = None

    if resolved_address is None:
        raise InvalidEventDataError(
            "Отсутствует адрес площадки и не удалось определить его по справочнику.", field_name="address"
        )

    date_start, date_end, duration_minutes = _parse_event_dates(
        raw_date_start=fields.get("date_start"),
        raw_date_end=fields.get("date_end"),
        raw_duration=fields.get("duration_minutes"),
        reference_datetime=reference_datetime,
    )

    category = parse_category(
        _as_optional_string(fields.get("category")), fallback_text=f"{cleaned_title} {cleaned_description}"
    )

    price_minimum, price_maximum, is_free = _parse_pricing(
        raw_price_minimum=fields.get("price_minimum"),
        raw_price_maximum=fields.get("price_maximum"),
        raw_price_text=fields.get("price"),
        raw_is_free=fields.get("is_free"),
    )

    age_limit = _parse_row_age_limit(
        raw_age_limit=fields.get("age_limit"), fallback_text=f"{cleaned_title} {cleaned_description}"
    )

    language = _normalize_explicit_or_inferred_language(
        raw_language=fields.get("language"), fallback_text=f"{cleaned_title} {cleaned_description}"
    )

    organization_link = _validate_optional_http_url(fields.get("organization_link"), field_name="organization_link")
    source_url = _validate_required_source_url(
        raw_source_url=fields.get("source_url"), fallback_organization_link=organization_link
    )

    latitude = _parse_optional_coordinate(fields.get("latitude"), field_name="latitude", minimum=-90.0, maximum=90.0)
    longitude = _parse_optional_coordinate(
        fields.get("longitude"), field_name="longitude", minimum=-180.0, maximum=180.0
    )
    if latitude is None and longitude is None:
        latitude = default_latitude
        longitude = default_longitude

    requested_status = _parse_row_status(fields.get("status"))
    effective_status = EventStatus.DRAFT if has_suspicious_instructions else requested_status

    explicit_tags = _parse_explicit_tags(fields.get("tags"))
    inferred_tags = extract_event_tags(
        raw_title=cleaned_title,
        raw_description=cleaned_description,
        category=category,
        is_free=is_free,
        language=language,
        age_limit=age_limit,
    )
    normalized_tags = tuple(sorted(set(explicit_tags) | set(inferred_tags)))

    source_name = _as_optional_string(fields.get("source_name")) or default_source_name
    normalized_title = normalize_title(cleaned_title)
    deduplication_key = compute_deduplication_key(
        normalized_title=normalized_title, date_start=date_start, city=resolved_city, venue=resolved_venue
    )

    hash_payload = _build_canonical_hash_payload(
        title=cleaned_title,
        category=category.value,
        date_start=date_start.isoformat(),
        date_end=date_end.isoformat() if date_end else None,
        duration_minutes=duration_minutes,
        city=resolved_city,
        venue=resolved_venue,
        address=resolved_address,
        description=cleaned_description,
        price_minimum=price_minimum,
        price_maximum=price_maximum,
        is_free=is_free,
        age_limit=age_limit,
        language=language,
        tags=list(normalized_tags),
        organization_link=organization_link,
        source_url=source_url,
        source_name=source_name,
        status=effective_status.value,
    )
    source_hash = compute_payload_hash(hash_payload)

    source_reference = EventSourceReference(
        source_name=source_name,
        source_type=default_source_type,
        source_url=source_url,
        source_hash=source_hash,
        verified_at=reference_datetime,
    )

    candidate_event = Event(
        identifier=explicit_identifier or uuid4(),
        title=cleaned_title,
        normalized_title=normalized_title,
        category=category,
        tags=normalized_tags,
        date_start=date_start,
        date_end=date_end,
        duration_minutes=duration_minutes,
        city=resolved_city,
        venue=resolved_venue,
        address=resolved_address,
        latitude=latitude,
        longitude=longitude,
        description=cleaned_description,
        price_minimum=price_minimum,
        price_maximum=price_maximum,
        is_free=is_free,
        age_limit=age_limit,
        language=language,
        organization_link=organization_link,
        source_url=source_url,
        source_type=default_source_type,
        source_hash=source_hash,
        verified_at=reference_datetime,
        confidence=1.0,
        status=effective_status,
        deduplication_key=deduplication_key,
        created_at=reference_datetime,
        updated_at=reference_datetime,
        sources=(source_reference,),
    )

    return _ParsedImportRecord(
        row_number=row_number,
        external_identifier=external_identifier or cleaned_title,
        explicit_identifier=explicit_identifier,
        candidate_event=candidate_event,
        has_suspicious_instructions=has_suspicious_instructions,
        matched_injection_patterns=matched_patterns,
    )


def _as_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return normalize_optional_text(value)
    return normalize_optional_text(str(value))


def _parse_optional_uuid(value: object) -> UUID | None:
    cleaned = _as_optional_string(value)
    if cleaned is None:
        return None
    try:
        return UUID(cleaned)
    except ValueError:
        return None


def _parse_datetime_value(value: object, *, reference_datetime: datetime, field_name: str) -> datetime:
    cleaned = _as_optional_string(value)
    if cleaned is None:
        raise InvalidEventDataError(f"Отсутствует дата в поле «{field_name}».", field_name=field_name)

    try:
        parsed_iso = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return attach_default_timezone(parsed_iso)
    except ValueError:
        parsed_start, _, _ = parse_event_datetime(cleaned, reference_datetime=reference_datetime)
        if parsed_start is None:
            raise InvalidEventDataError(
                f"Не удалось распознать дату «{cleaned}» в поле «{field_name}».", field_name=field_name
            ) from None
        return parsed_start


def _parse_event_dates(
    *, raw_date_start: object, raw_date_end: object, raw_duration: object, reference_datetime: datetime
) -> tuple[datetime, datetime | None, int | None]:
    raw_start_text = _as_optional_string(raw_date_start)
    if raw_start_text is None:
        raise InvalidEventDataError("Отсутствует обязательная дата начала мероприятия.", field_name="date_start")

    date_end: datetime | None = None
    inferred_duration: int | None = None

    try:
        parsed_iso = datetime.fromisoformat(raw_start_text.replace("Z", "+00:00"))
        date_start = attach_default_timezone(parsed_iso)
    except ValueError:
        parsed_start, parsed_end, parsed_duration = parse_event_datetime(
            raw_start_text, reference_datetime=reference_datetime
        )
        if parsed_start is None:
            raise InvalidEventDataError(
                f"Не удалось распознать дату начала «{raw_start_text}».", field_name="date_start"
            ) from None
        date_start = parsed_start
        date_end = parsed_end
        inferred_duration = parsed_duration

    if _as_optional_string(raw_date_end) is not None:
        date_end = _parse_datetime_value(raw_date_end, reference_datetime=reference_datetime, field_name="date_end")

    if date_end is not None and date_end < date_start:
        raise InvalidEventDataError(
            "Дата окончания мероприятия не может быть раньше даты начала.", field_name="date_end"
        )

    explicit_duration: int | None = None
    cleaned_duration = _as_optional_string(raw_duration)
    if cleaned_duration is not None:
        try:
            explicit_duration = int(cleaned_duration)
        except ValueError as error:
            raise InvalidEventDataError(
                f"Длительность «{cleaned_duration}» должна быть целым числом минут.", field_name="duration_minutes"
            ) from error

    duration_minutes = (
        resolve_duration_minutes(date_start=date_start, date_end=date_end, explicit_duration_minutes=explicit_duration)
        if explicit_duration is not None or (date_end is not None and date_end > date_start)
        else inferred_duration
    )

    return date_start, date_end, duration_minutes


def _parse_optional_non_negative_int(value: object, *, field_name: str) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0:
            raise InvalidEventDataError(f"Поле «{field_name}» не может быть отрицательным.", field_name=field_name)
        return value
    cleaned = _as_optional_string(value)
    if cleaned is None:
        return None
    try:
        parsed = int(float(cleaned.replace(",", ".")))
    except ValueError as error:
        raise InvalidEventDataError(
            f"Значение «{cleaned}» в поле «{field_name}» должно быть числом.", field_name=field_name
        ) from error
    if parsed < 0:
        raise InvalidEventDataError(f"Поле «{field_name}» не может быть отрицательным.", field_name=field_name)
    return parsed


def _parse_optional_boolean(value: object, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    cleaned = _as_optional_string(value)
    if cleaned is None:
        return None
    lowered = cleaned.lower()
    if lowered in _BOOLEAN_TRUE_VALUES:
        return True
    if lowered in _BOOLEAN_FALSE_VALUES:
        return False
    raise InvalidEventDataError(
        f"Не удалось распознать логическое значение «{cleaned}» в поле «{field_name}».", field_name=field_name
    )


def _parse_pricing(
    *, raw_price_minimum: object, raw_price_maximum: object, raw_price_text: object, raw_is_free: object
) -> tuple[int | None, int | None, bool]:
    explicit_minimum = _parse_optional_non_negative_int(raw_price_minimum, field_name="price_minimum")
    explicit_maximum = _parse_optional_non_negative_int(raw_price_maximum, field_name="price_maximum")
    explicit_free = _parse_optional_boolean(raw_is_free, field_name="is_free")

    parsed_minimum, parsed_maximum, parsed_free = parse_price_range(_as_optional_string(raw_price_text))

    price_minimum = explicit_minimum if explicit_minimum is not None else parsed_minimum
    price_maximum = explicit_maximum if explicit_maximum is not None else parsed_maximum

    if explicit_free is True:
        is_free = True
        if price_minimum is None:
            price_minimum = 0
        if price_maximum is None:
            price_maximum = 0
    elif explicit_free is False:
        is_free = False
    elif parsed_free is not None:
        is_free = parsed_free
    else:
        is_free = price_minimum == 0 and (price_maximum is None or price_maximum == 0)

    if price_minimum is not None and price_maximum is not None and price_minimum > price_maximum:
        raise InvalidEventDataError(
            "Минимальная цена билета не может превышать максимальную.", field_name="price_minimum"
        )

    return price_minimum, price_maximum, is_free


def _parse_row_age_limit(*, raw_age_limit: object, fallback_text: str) -> int:
    if isinstance(raw_age_limit, int) and not isinstance(raw_age_limit, bool):
        if raw_age_limit not in _ALLOWED_AGE_LIMITS:
            raise InvalidEventDataError(
                f"Возрастное ограничение {raw_age_limit} должно быть одним из 0, 6, 12, 16, 18.", field_name="age_limit"
            )
        return raw_age_limit

    cleaned = _as_optional_string(raw_age_limit)
    if cleaned is not None and cleaned.isdigit():
        numeric_age = int(cleaned)
        if numeric_age not in _ALLOWED_AGE_LIMITS:
            raise InvalidEventDataError(
                f"Возрастное ограничение {numeric_age} должно быть одним из 0, 6, 12, 16, 18.", field_name="age_limit"
            )
        return numeric_age

    parsed_age = parse_age_limit(cleaned, fallback_text=fallback_text)
    if cleaned is not None and parsed_age is None:
        raise InvalidEventDataError(
            f"Не удалось распознать возрастное ограничение «{cleaned}».", field_name="age_limit"
        )
    return parsed_age if parsed_age is not None else 0


def _normalize_explicit_or_inferred_language(*, raw_language: object, fallback_text: str) -> str | None:
    cleaned = _as_optional_string(raw_language)
    if cleaned is not None:
        return parse_language(cleaned) or cleaned.lower()
    return parse_language(None, fallback_text=fallback_text)


def _validate_optional_http_url(value: object, *, field_name: str) -> str | None:
    cleaned = _as_optional_string(value)
    if cleaned is None:
        return None
    return validate_http_url(cleaned, field_name=field_name)


def _validate_required_source_url(*, raw_source_url: object, fallback_organization_link: str | None) -> str:
    validated_source = _validate_optional_http_url(raw_source_url, field_name="source_url")
    effective_url = validated_source or fallback_organization_link
    if effective_url is None:
        raise InvalidEventDataError("Отсутствует обязательная ссылка на источник мероприятия.", field_name="source_url")
    return effective_url


def _parse_optional_coordinate(value: object, *, field_name: str, minimum: float, maximum: float) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        cleaned = _as_optional_string(value)
        if cleaned is None:
            return None
        try:
            numeric = float(cleaned.replace(",", "."))
        except ValueError as error:
            raise InvalidEventDataError(
                f"Некорректное значение координаты в поле «{field_name}».", field_name=field_name
            ) from error
    if not (minimum <= numeric <= maximum):
        raise InvalidEventDataError(
            f"Координата в поле «{field_name}» выходит за допустимые пределы [{minimum}; {maximum}].",
            field_name=field_name,
        )
    return numeric


def _parse_row_status(value: object) -> EventStatus:
    cleaned = _as_optional_string(value)
    if cleaned is None:
        return EventStatus.PUBLISHED
    resolved = _STATUS_MAP.get(cleaned.lower())
    if resolved is None:
        raise InvalidEventDataError(f"Недопустимый статус мероприятия «{cleaned}».", field_name="status")
    return resolved


def _parse_explicit_tags(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_tokens: list[str]
    if isinstance(value, str):
        raw_tokens = value.replace(";", ",").split(",")
    elif isinstance(value, Sequence):
        raw_tokens = [str(item) for item in value]
    else:
        raw_tokens = [str(value)]

    cleaned_tags: list[str] = []
    for token in raw_tokens:
        normalized_token = normalize_optional_text(token)
        if normalized_token is not None:
            lowered = normalized_token.lower()
            if lowered not in cleaned_tags:
                cleaned_tags.append(lowered)
    return tuple(cleaned_tags)


def _build_canonical_hash_payload(
    *,
    title: str,
    category: str,
    date_start: str,
    date_end: str | None,
    duration_minutes: int | None,
    city: str,
    venue: str,
    address: str,
    description: str,
    price_minimum: int | None,
    price_maximum: int | None,
    is_free: bool,
    age_limit: int,
    language: str | None,
    tags: list[str],
    organization_link: str | None,
    source_url: str,
    source_name: str,
    status: str,
) -> dict[str, object]:
    return {
        "title": title,
        "category": category,
        "date_start": date_start,
        "date_end": date_end,
        "duration_minutes": duration_minutes,
        "city": city,
        "venue": venue,
        "address": address,
        "description": description,
        "price_minimum": price_minimum,
        "price_maximum": price_maximum,
        "is_free": is_free,
        "age_limit": age_limit,
        "language": language,
        "tags": tags,
        "organization_link": organization_link,
        "source_url": source_url,
        "source_name": source_name,
        "status": status,
    }


def _is_unchanged_import_match(*, existing_event: Event, candidate: Event) -> bool:
    if existing_event.source_hash == candidate.source_hash:
        return True
    return any(
        source_item.source_type == candidate.source_type and source_item.source_hash == candidate.source_hash
        for source_item in existing_event.sources
    )


def _build_updated_same_source_event(
    *, existing_event: Event, candidate: Event, updated_at: datetime, force_draft: bool
) -> Event:
    new_source = candidate.sources[0]
    preserved_sources: list[EventSourceReference] = [new_source]
    for source_item in existing_event.sources:
        if source_item.source_url == new_source.source_url and source_item.source_type == new_source.source_type:
            continue
        if (source_item.source_type, source_item.source_hash) == (new_source.source_type, new_source.source_hash):
            continue
        preserved_sources.append(source_item)

    target_status = EventStatus.DRAFT if force_draft else candidate.status
    return Event(
        identifier=existing_event.identifier,
        title=candidate.title,
        normalized_title=candidate.normalized_title,
        category=candidate.category,
        tags=candidate.tags,
        date_start=candidate.date_start,
        date_end=candidate.date_end,
        duration_minutes=candidate.duration_minutes,
        city=candidate.city,
        venue=candidate.venue,
        address=candidate.address,
        latitude=candidate.latitude if candidate.latitude is not None else existing_event.latitude,
        longitude=candidate.longitude if candidate.longitude is not None else existing_event.longitude,
        description=candidate.description,
        price_minimum=candidate.price_minimum,
        price_maximum=candidate.price_maximum,
        is_free=candidate.is_free,
        age_limit=candidate.age_limit,
        language=candidate.language,
        organization_link=candidate.organization_link,
        source_url=candidate.source_url,
        source_type=candidate.source_type,
        source_hash=candidate.source_hash,
        verified_at=candidate.verified_at,
        confidence=candidate.confidence,
        status=target_status,
        deduplication_key=candidate.deduplication_key,
        created_at=existing_event.created_at,
        updated_at=updated_at,
        sources=tuple(preserved_sources),
    )

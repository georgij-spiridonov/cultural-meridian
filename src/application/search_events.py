"""Прикладной сервис поиска, фильтрации и карточки мероприятий."""

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from src.application._helpers import resolve_normalized_city
from src.core.types import EventCategory, EventStatus, RealCollection
from src.domain.entities import (
    CityReference,
    EmptySearchExplanation,
    Event,
    EventSearchFilter,
    EventSearchPage,
    VenueReference,
)
from src.domain.exceptions import EventNotFoundError, InvalidSearchFilterError
from src.domain.ports import EventRepositoryPort
from src.domain.services import normalize_optional_text, normalize_venue

__all__ = ["EventSearchService"]

_MAXIMUM_PAGE_SIZE = 100
_MAXIMUM_SIMILAR_LIMIT = 20

_CATEGORY_DISPLAY_NAMES: dict[EventCategory, str] = {
    EventCategory.CONCERT: "Концерты",
    EventCategory.SPECTACLE: "Спектакли",
    EventCategory.EXHIBITION: "Выставки",
    EventCategory.FESTIVAL: "Фестивали",
    EventCategory.CINEMA: "Кино",
    EventCategory.EXCURSION: "Экскурсии",
    EventCategory.OTHER: "Другое",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class _RelaxationCandidate:
    """Вариант ослабления одного ограничения фильтра для диагностики пустой выдачи."""

    blocking_label: str
    suggestion_text: str
    relaxed_filter: EventSearchFilter


class EventSearchService:
    """Прикладной сценарий поиска, фильтрации и просмотра карточек мероприятий."""

    def __init__(self, *, event_repository: EventRepositoryPort) -> None:
        """Инициализирует сервис поиска с опорой на порт репозитория мероприятий.

        Arguments:
            event_repository: Реализация порта каталога мероприятий.
        """
        self._event_repository = event_repository

    async def search_events(
        self,
        *,
        search_filter: EventSearchFilter | None = None,
        include_empty_explanation: bool = True,
        statuses: RealCollection[EventStatus] = (EventStatus.PUBLISHED,),
    ) -> EventSearchPage:
        """Выполняет валидированный поиск мероприятий по фильтру и формирует пояснение пустой выдачи.

        Arguments:
            search_filter: Параметры поиска и пагинации. При `None` используются значения по умолчанию.
            include_empty_explanation: Признак необходимости расчёта подсказок при нулевой выдаче.
            statuses: Допустимые статусы публикаций для выборки.

        Returns:
            Страница найденных мероприятий с общим количеством и диагностикой пустой выдачи.

        Raises:
            InvalidSearchFilterError: При противоречивых или некорректных параметрах фильтра.
        """
        effective_filter = search_filter or EventSearchFilter()
        self._validate_search_filter(effective_filter, statuses=statuses)
        normalized_filter = await self._normalize_search_filter(effective_filter)

        search_page = await self._event_repository.search_events(search_filter=normalized_filter, statuses=statuses)
        if search_page.total_count > 0 or not include_empty_explanation or EventStatus.PUBLISHED not in statuses:
            return search_page

        empty_explanation = await self._build_empty_explanation(normalized_filter)
        return replace(search_page, empty_explanation=empty_explanation)

    async def get_event(self, *, event_identifier: UUID, allow_draft: bool = False) -> Event:
        """Возвращает карточку мероприятия по идентификатору.

        По умолчанию возвращает опубликованные и отменённые карточки (чтобы отмена корректно
        отображалась по прямой ссылке), но скрывает черновики из публичного доступа.

        Arguments:
            event_identifier: Уникальный идентификатор мероприятия.
            allow_draft: Разрешать ли возврат карточки со статусом черновика.

        Returns:
            Доменная карточка мероприятия.

        Raises:
            EventNotFoundError: Если карточка не найдена или является скрытым черновиком.
        """
        event = await self._event_repository.get_event_by_identifier(
            event_identifier=event_identifier, include_non_published=True
        )
        if event.status is EventStatus.DRAFT and not allow_draft:
            raise EventNotFoundError(
                f"Мероприятие {event_identifier} не опубликовано в открытом каталоге.",
                event_identifier=event_identifier,
                reason="Карточка находится в черновиках и ожидает проверки",
            )
        return event

    async def find_similar_events(self, *, event_identifier: UUID, limit: int = 5) -> tuple[Event, ...]:
        """Подбирает похожие опубликованные мероприятия для выбранной карточки.

        Arguments:
            event_identifier: Идентификатор исходного мероприятия.
            limit: Максимальное количество рекомендаций (`1..20`).

        Returns:
            Кортеж похожих мероприятий.

        Raises:
            InvalidSearchFilterError: Если лимит выходит за допустимые пределы.
            EventNotFoundError: Если исходное мероприятие не найдено в публичном каталоге.
        """
        if limit < 1 or limit > _MAXIMUM_SIMILAR_LIMIT:
            raise InvalidSearchFilterError(
                f"Количество похожих мероприятий должно быть от 1 до {_MAXIMUM_SIMILAR_LIMIT}.",
                parameter_name="limit",
                reason="Некорректное значение ограничения выборки",
            )

        target_event = await self.get_event(event_identifier=event_identifier)
        return await self._event_repository.find_similar_events(
            event_identifier=target_event.identifier, city=target_event.city, limit=limit
        )

    async def get_venue_schedule(
        self,
        *,
        venue: str,
        city: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> EventSearchPage:
        """Возвращает афишу выбранной культурной площадки.

        Arguments:
            venue: Название площадки или его часть.
            city: Необязательное уточнение города.
            date_from: Нижняя граница времени начала мероприятий.
            date_to: Верхняя граница времени начала мероприятий.
            page: Номер страницы выборки.
            page_size: Количество карточек на странице.

        Returns:
            Страница расписания площадки.

        Raises:
            InvalidSearchFilterError: Если название площадки пустое или фильтр некорректен.
        """
        cleaned_venue = normalize_optional_text(venue)
        if cleaned_venue is None:
            raise InvalidSearchFilterError(
                "Название площадки для расписания не может быть пустым.",
                parameter_name="venue",
                reason="Отсутствует название площадки",
            )

        schedule_filter = EventSearchFilter(
            city=city, venue=cleaned_venue, date_from=date_from, date_to=date_to, page=page, page_size=page_size
        )
        return await self.search_events(search_filter=schedule_filter)

    async def list_cities(self) -> tuple[CityReference, ...]:
        """Возвращает нормализованный справочник городов каталога."""
        return await self._event_repository.list_cities()

    async def list_languages(self) -> tuple[str, ...]:
        """Возвращает отсортированный список языков опубликованных мероприятий."""
        return await self._event_repository.list_languages()

    async def list_venues(self, *, city: str | None = None) -> tuple[VenueReference, ...]:
        """Возвращает справочник площадок с необязательной фильтрацией по городу.

        Arguments:
            city: Название города (допускает синонимы из справочника).

        Returns:
            Кортеж записей справочника площадок.
        """
        cleaned_city = normalize_optional_text(city)
        if cleaned_city is not None:
            resolved_city: str | None = await resolve_normalized_city(
                cleaned_city, event_repository=self._event_repository
            )
        else:
            resolved_city = None
        return await self._event_repository.list_venues(city=resolved_city)

    async def get_catalog_fingerprint(self) -> str:
        """Возвращает отпечаток текущего состояния опубликованного каталога для кэширования."""
        return await self._event_repository.get_catalog_fingerprint()

    @staticmethod
    def _validate_search_filter(search_filter: EventSearchFilter, *, statuses: RealCollection[EventStatus]) -> None:
        if not statuses:
            raise InvalidSearchFilterError(
                "Список статусов выборки не может быть пустым.",
                parameter_name="statuses",
                reason="Не указан ни один допустимый статус публикации",
            )
        if search_filter.page < 1:
            raise InvalidSearchFilterError(
                "Номер страницы выдачи должен быть не меньше 1.",
                parameter_name="page",
                reason="Передан неположительный номер страницы",
            )
        if search_filter.page_size < 1 or search_filter.page_size > _MAXIMUM_PAGE_SIZE:
            raise InvalidSearchFilterError(
                f"Размер страницы должен находиться в диапазоне от 1 до {_MAXIMUM_PAGE_SIZE}.",
                parameter_name="page_size",
                reason="Превышены допустимые границы размера страницы",
            )
        if (
            search_filter.date_from is not None
            and search_filter.date_to is not None
            and search_filter.date_from > search_filter.date_to
        ):
            raise InvalidSearchFilterError(
                "Начальная дата поиска не может быть позже конечной даты.",
                parameter_name="date_from",
                reason="Перевёрнутый диапазон дат",
            )
        if (
            search_filter.time_from is not None
            and search_filter.time_to is not None
            and search_filter.time_from > search_filter.time_to
        ):
            raise InvalidSearchFilterError(
                "Начальное время суток не может быть позже конечного времени.",
                parameter_name="time_from",
                reason="Перевёрнутый суточный интервал времени",
            )
        if search_filter.maximum_price is not None and search_filter.maximum_price < 0:
            raise InvalidSearchFilterError(
                "Максимальная цена билета не может быть отрицательной.",
                parameter_name="maximum_price",
                reason="Указана отрицательная цена",
            )
        if search_filter.maximum_age_limit is not None and search_filter.maximum_age_limit < 0:
            raise InvalidSearchFilterError(
                "Максимальное возрастное ограничение не может быть отрицательным.",
                parameter_name="maximum_age_limit",
                reason="Указан отрицательный возраст",
            )
        if search_filter.maximum_duration_minutes is not None and search_filter.maximum_duration_minutes <= 0:
            raise InvalidSearchFilterError(
                "Максимальная длительность мероприятия должна быть больше 0 минут.",
                parameter_name="maximum_duration_minutes",
                reason="Неположительная длительность мероприятия",
            )

    async def _normalize_search_filter(self, search_filter: EventSearchFilter) -> EventSearchFilter:
        cleaned_query = normalize_optional_text(search_filter.query)
        cleaned_city = normalize_optional_text(search_filter.city)
        cleaned_venue = normalize_optional_text(search_filter.venue)
        cleaned_language = normalize_optional_text(search_filter.language)

        resolved_city = (
            await resolve_normalized_city(cleaned_city, event_repository=self._event_repository)
            if cleaned_city is not None
            else None
        )

        resolved_venue = cleaned_venue
        if cleaned_venue is not None:
            venues = await self._event_repository.list_venues(city=resolved_city)
            normalized_venue = normalize_venue(cleaned_venue, venues=venues, city=resolved_city)
            if isinstance(normalized_venue, VenueReference):
                resolved_venue = normalized_venue.name
            elif normalized_venue is not None:
                resolved_venue = normalized_venue

        normalized_language = cleaned_language.lower() if cleaned_language is not None else None
        return replace(
            search_filter, query=cleaned_query, city=resolved_city, venue=resolved_venue, language=normalized_language
        )

    async def _build_empty_explanation(self, normalized_filter: EventSearchFilter) -> EmptySearchExplanation:
        candidates = self._build_relaxation_candidates(normalized_filter)
        blocking_constraints: list[str] = []
        suggested_relaxations: list[str] = []

        for candidate in candidates:
            relaxed_count = await self._event_repository.count_events(search_filter=candidate.relaxed_filter)
            if relaxed_count > 0:
                blocking_constraints.append(candidate.blocking_label)
                suggested_relaxations.append(f"{candidate.suggestion_text} (найдётся {relaxed_count})")

        if suggested_relaxations:
            summary = (
                "По точному сочетанию параметров ничего не найдено. "
                f"Результат отсеяли ограничения: {', '.join(blocking_constraints[:3])}."
            )
            return EmptySearchExplanation(
                summary=summary,
                blocking_constraints=tuple(blocking_constraints),
                suggested_relaxations=tuple(suggested_relaxations),
            )

        active_labels = tuple(candidate.blocking_label for candidate in candidates)
        total_published = await self._event_repository.count_events(search_filter=EventSearchFilter())
        if total_published == 0:
            return EmptySearchExplanation(
                summary="В каталоге пока нет опубликованных мероприятий.",
                blocking_constraints=active_labels,
                suggested_relaxations=(
                    "Попробуйте загрузить афишу через импорт или запустить сбор внешних источников.",
                ),
            )

        suggestions: tuple[str, ...]
        if active_labels:
            summary = (
                "Сочетание сразу нескольких фильтров полностью исключило выдачу "
                f"(всего в каталоге опубликовано {total_published} событий)."
            )
            suggestions = (
                "Снимите два или более ограничения одновременно (например, расширьте даты и уберите запрос).",
            )
        else:
            summary = "Подходящих опубликованных мероприятий не найдено."
            suggestions = ()

        return EmptySearchExplanation(
            summary=summary, blocking_constraints=active_labels, suggested_relaxations=suggestions
        )

    @staticmethod
    def _build_relaxation_candidates(normalized_filter: EventSearchFilter) -> tuple[_RelaxationCandidate, ...]:
        base_filter = replace(normalized_filter, page=1)
        candidates: list[_RelaxationCandidate] = []

        if base_filter.only_free:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label="только бесплатные мероприятия",
                    suggestion_text="Разрешить платные мероприятия (убрать фильтр «бесплатно»)",
                    relaxed_filter=replace(base_filter, only_free=False),
                )
            )
        if base_filter.maximum_price is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label=f"цена до {base_filter.maximum_price} ₽",
                    suggestion_text=f"Увеличить или снять ограничение бюджета (до {base_filter.maximum_price} ₽)",
                    relaxed_filter=replace(base_filter, maximum_price=None),
                )
            )
        if base_filter.maximum_age_limit is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label=f"возрастной ценз до {base_filter.maximum_age_limit}+",
                    suggestion_text=f"Снять возрастное ограничение ({base_filter.maximum_age_limit}+)",
                    relaxed_filter=replace(base_filter, maximum_age_limit=None),
                )
            )
        if base_filter.maximum_duration_minutes is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label=f"длительность до {base_filter.maximum_duration_minutes} мин.",
                    suggestion_text=(
                        f"Увеличить максимальную длительность (более {base_filter.maximum_duration_minutes} мин.)"
                    ),
                    relaxed_filter=replace(base_filter, maximum_duration_minutes=None),
                )
            )
        if base_filter.time_from is not None or base_filter.time_to is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label="ограничение по времени начала",
                    suggestion_text="Расширить временное окно начала мероприятий",
                    relaxed_filter=replace(base_filter, time_from=None, time_to=None),
                )
            )
        if base_filter.venue is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label=f"площадка «{base_filter.venue}»",
                    suggestion_text=f"Искать на всех площадках вместо «{base_filter.venue}»",
                    relaxed_filter=replace(base_filter, venue=None),
                )
            )
        if base_filter.language is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label=f"язык «{base_filter.language}»",
                    suggestion_text=f"Убрать фильтр по языку («{base_filter.language}»)",
                    relaxed_filter=replace(base_filter, language=None),
                )
            )
        if base_filter.category is not None:
            category_label = _CATEGORY_DISPLAY_NAMES.get(base_filter.category, base_filter.category.value)
            candidates.append(
                _RelaxationCandidate(
                    blocking_label=f"категория «{category_label}»",
                    suggestion_text=f"Показать события всех категорий вместо «{category_label}»",
                    relaxed_filter=replace(base_filter, category=None),
                )
            )
        if base_filter.date_from is not None or base_filter.date_to is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label="выбранный диапазон дат",
                    suggestion_text="Расширить диапазон дат или выбрать соседние дни",
                    relaxed_filter=replace(base_filter, date_from=None, date_to=None),
                )
            )
        if base_filter.query is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label=f"запрос «{base_filter.query}»",
                    suggestion_text=f"Убрать или упростить текстовый запрос «{base_filter.query}»",
                    relaxed_filter=replace(base_filter, query=None),
                )
            )
        if base_filter.city is not None:
            candidates.append(
                _RelaxationCandidate(
                    blocking_label=f"город «{base_filter.city}»",
                    suggestion_text=f"Расширить поиск на соседние города вместо «{base_filter.city}»",
                    relaxed_filter=replace(base_filter, city=None),
                )
            )

        return tuple(candidates)

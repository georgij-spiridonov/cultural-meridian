"""Общие внутренние вспомогательные функции для PostgreSQL-репозиториев."""

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ValidationException
from src.domain.services.normalization import normalize_city, normalize_optional_text, normalize_text_for_matching
from src.infrastructure.database.tables import CityAliasRow, CityRow

__all__ = ["build_city_match_condition", "resolve_or_create_city", "validate_positive_limit"]


def validate_positive_limit(limit: int, *, parameter_name: str = "limit", maximum: int = 1000) -> int:
    """Проверяет положительность и допустимый верхний предел размера выборки.

    Arguments:
        limit: Запрошенное максимальное число записей.
        parameter_name: Имя параметра для диагностического сообщения.
        maximum: Максимально допустимое значение выборки.

    Returns:
        Проверенное значение предела выборки.

    Raises:
        ValidationException: Если `limit` меньше 1 или превышает `maximum`.
    """
    if limit < 1 or limit > maximum:
        raise ValidationException(
            f"Параметр «{parameter_name}» должен находиться в диапазоне от 1 до {maximum}, получено: {limit}."
        )
    return limit


def build_city_match_condition(raw_city_query: str) -> ColumnElement[bool] | None:
    """Строит SQL-условие поиска города по каноническому имени, алиасам и триграммам.

    Arguments:
        raw_city_query: Исходная строка города от пользователя или фильтра поиска.

    Returns:
        SQL-предикат для таблицы `cities` либо `None`, если строка пуста.
    """
    cleaned_input = normalize_optional_text(raw_city_query)
    if cleaned_input is None:
        return None

    canonical_candidate = normalize_city(cleaned_input) or cleaned_input
    normalized_canonical = normalize_text_for_matching(canonical_candidate)
    normalized_raw = normalize_text_for_matching(cleaned_input)

    candidates = tuple({value for value in (normalized_canonical, normalized_raw) if value})
    if not candidates:
        return None

    exact_match = CityRow.normalized_name.in_(candidates)
    alias_match = exists(
        select(1).where(
            (CityAliasRow.city_identifier == CityRow.identifier) & (CityAliasRow.normalized_alias.in_(candidates))
        )
    )
    trigram_match = and_(
        CityRow.normalized_name.op("%")(normalized_canonical),
        func.similarity(CityRow.normalized_name, normalized_canonical) >= 0.45,
    )
    return or_(exact_match, alias_match, trigram_match)


async def resolve_or_create_city(session: AsyncSession, *, city_name: str) -> CityRow:
    """Находит существующий город по имени или алиасу либо идемпотентно создаёт запись.

    Arguments:
        session: Активная асинхронная сессия SQLAlchemy.
        city_name: Каноническое или очищенное название города.

    Returns:
        ORM-строка города в таблице `cities`.

    Raises:
        ValidationException: Если название города пусто после очистки пробелов.
    """
    cleaned_name = normalize_optional_text(city_name)
    if cleaned_name is None:
        raise ValidationException("Название города не может быть пустым.")

    canonical_name = normalize_city(cleaned_name) or cleaned_name
    normalized_name = normalize_text_for_matching(canonical_name)
    if not normalized_name:
        raise ValidationException("Не удалось нормализовать название города.")

    existing_by_name = (
        await session.execute(select(CityRow).where(CityRow.normalized_name == normalized_name))
    ).scalar_one_or_none()
    if existing_by_name is not None:
        return existing_by_name

    raw_normalized = normalize_text_for_matching(cleaned_name)
    if raw_normalized:
        alias_row = (
            await session.execute(select(CityAliasRow).where(CityAliasRow.normalized_alias == raw_normalized))
        ).scalar_one_or_none()
        if alias_row is not None:
            city_by_alias = (
                await session.execute(select(CityRow).where(CityRow.identifier == alias_row.city_identifier))
            ).scalar_one_or_none()
            if city_by_alias is not None:
                return city_by_alias

    await session.execute(
        postgresql_insert(CityRow).values(name=canonical_name, normalized_name=normalized_name).on_conflict_do_nothing()
    )
    resolved = (
        await session.execute(
            select(CityRow).where(or_(CityRow.normalized_name == normalized_name, CityRow.name == canonical_name))
        )
    ).scalar_one()
    return resolved

"""Внутренние вспомогательные функции для глубокой неизменяемости и сериализации сущностей."""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final, cast

__all__ = ["EMPTY_OBJECT_MAPPING", "freeze_mapping", "restore_dataclass_state", "serialize_dataclass_state"]

EMPTY_OBJECT_MAPPING: Final[Mapping[str, object]] = MappingProxyType({})


def freeze_mapping[KeyType, ValueType](mapping: Mapping[KeyType, ValueType] | None) -> Mapping[KeyType, ValueType]:
    """Преобразует произвольное отображение в неизменяемую обёртку `MappingProxyType`.

    Arguments:
        mapping: Исходное отображение или `None`.

    Returns:
        Неизменяемая обёртка над копией исходного отображения.
    """
    if not mapping:
        return MappingProxyType({})
    return MappingProxyType(dict(mapping))


def serialize_dataclass_state(instance: object) -> dict[str, object]:
    """Подготавливает состояние неизменяемого датакласса для `pickle` и `deepcopy`.

    Преобразует `MappingProxyType` обратно в обычный `dict`, так как стандартный
    модуль `pickle` не поддерживает сериализацию `mappingproxy`.

    Arguments:
        instance: Экземпляр датакласса, состояние которого требуется сериализовать.

    Returns:
        Словарь значений полей датакласса, пригодный для сериализации.
    """
    dataclass_fields = cast("Mapping[str, object]", getattr(type(instance), "__dataclass_fields__", {}))
    state: dict[str, object] = {}
    for field_name in dataclass_fields:
        value: object = getattr(instance, field_name)
        if isinstance(value, MappingProxyType):
            state[field_name] = dict(value)
        else:
            state[field_name] = value
    return state


def restore_dataclass_state(instance: object, state: Mapping[str, object], post_init: Callable[[], None]) -> None:
    """Восстанавливает поля замороженного датакласса после десериализации.

    Arguments:
        instance: Восстанавливаемый экземпляр датакласса.
        state: Словарь сохранённых значений полей.
        post_init: Метод `__post_init__` экземпляра для повторной заморозки коллекций.
    """
    for field_name, field_value in state.items():
        object.__setattr__(instance, field_name, field_value)
    post_init()

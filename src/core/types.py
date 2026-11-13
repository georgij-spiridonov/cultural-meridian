"""Базовые универсальные типы и перечисления ядра приложения."""

from collections.abc import Set as AbstractSet
from enum import StrEnum
from typing import Literal

__all__ = [
    "DialogChannel",
    "EnvironmentType",
    "EventCategory",
    "EventStatus",
    "LoggingFileModeType",
    "LoggingLevelType",
    "RealCollection",
    "SourceType",
]

type RealCollection[T] = AbstractSet[T] | list[T] | tuple[T, ...]
type EnvironmentType = Literal["development", "production", "testing"]
type LoggingLevelType = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
type LoggingFileModeType = Literal["w", "a"]


class EventCategory(StrEnum):
    """Категория культурного мероприятия из справочника сервиса.

    Attributes:
        CONCERT: Концертная программа.
        SPECTACLE: Театральный спектакль.
        EXHIBITION: Выставка или экспозиция.
        FESTIVAL: Фестиваль.
        CINEMA: Кинопоказ.
        EXCURSION: Экскурсия.
        OTHER: Прочее культурное мероприятие.
    """

    CONCERT = "concert"
    SPECTACLE = "spectacle"
    EXHIBITION = "exhibition"
    FESTIVAL = "festival"
    CINEMA = "cinema"
    EXCURSION = "excursion"
    OTHER = "other"


class EventStatus(StrEnum):
    """Статус публикации карточки мероприятия в каталоге.

    Attributes:
        PUBLISHED: Опубликовано и доступно в публичной выдаче.
        DRAFT: Черновик или карточка в очереди модерации.
        CANCELED: Отменено; исключается из выдачи, но доступно по прямой ссылке.
    """

    PUBLISHED = "published"
    DRAFT = "draft"
    CANCELED = "canceled"


class SourceType(StrEnum):
    """Тип источника поступления сведений о мероприятии.

    Attributes:
        MANUAL: Ручной ввод через интерфейс администрирования.
        IMPORT_FILE: Импорт из файла CSV или JSON.
        RSS: Внешняя RSS-лента.
        API: Внешний программный интерфейс.
        WEBPAGE: Веб-страница учреждения культуры.
    """

    MANUAL = "manual"
    IMPORT_FILE = "import_file"
    RSS = "rss"
    API = "api"
    WEBPAGE = "webpage"


class DialogChannel(StrEnum):
    """Канал взаимодействия пользователя с сервисом.

    Attributes:
        WEB: Веб-интерфейс и виджет.
        TELEGRAM: Telegram-бот.
        EVALUATION: Автоматический контур оценки качества.
    """

    WEB = "web"
    TELEGRAM = "telegram"
    EVALUATION = "evaluation"

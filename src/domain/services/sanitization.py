"""Чистая доменная защита от инъекций инструкций и изоляция недоверенных текстов."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from src.core.types import RealCollection

__all__ = [
    "DEFAULT_PROTECTED_PROMPT_MARKERS",
    "SanitizationInspectionResult",
    "contains_system_prompt_leak",
    "inspect_untrusted_text",
    "sanitize_control_characters",
    "wrap_untrusted_event_data",
]

DEFAULT_PROTECTED_PROMPT_MARKERS: Final[tuple[str, ...]] = (
    "<untrusted_event_data>",
    "SYSTEM_INSTRUCTION_BOUNDARY",
    "CRITICAL_GROUNDING_RULE",
)

_CONTROL_CHARS_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\ufeff]")
_MULTISPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[ \t]{2,}")
_UNTRUSTED_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(r"</?untrusted_event_data>", flags=re.IGNORECASE)

_INJECTION_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = MappingProxyType(
    {
        "ignore_previous_instructions": re.compile(
            r"(?:игнорируй|проигнорируй|отмени|забудь)\s+(?:все\s+)?(?:предыдущие\s+)?(?:инструкции|правила|указания)"
            r"|(?:отключи|обойди|нарушь)\s+(?:все\s+)?(?:правила|ограничения|фильтры)"
            r"|не\s+(?:следуй|подчиняйся)\s+(?:инструкциям|правилам)"
            r"|(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|rules)",
            flags=re.IGNORECASE,
        ),
        "reveal_system_prompt": re.compile(
            r"(?:покажи|выведи|раскрой|назови|повтори)\s+(?:свой\s+|текущий\s+)?(?:системный\s+промпт|инструкции)"
            r"|(?:reveal|show|print|leak)\s+(?:your\s+)?(?:system\s+prompt|hidden\s+instructions)",
            flags=re.IGNORECASE,
        ),
        "publish_arbitrary_text": re.compile(
            r"(?:опубликуй|выведи|напиши)\s+(?:мой|следующий|произвольный)\s+текст", flags=re.IGNORECASE
        ),
        "role_override_marker": re.compile(
            r"(?:<\|im_start\|>|<\|system\|>|^\s*(?:system|developer)\s*:|ты\s+теперь\s+не\s+помощник)",
            flags=re.IGNORECASE | re.MULTILINE,
        ),
        "tag_breakout_attempt": re.compile(r"</untrusted_event_data>", flags=re.IGNORECASE),
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SanitizationInspectionResult:
    """Результат проверки и очистки текста на наличие попыток инъекции инструкций.

    Attributes:
        cleaned_text: Очищенный от управляющих символов текст.
        has_suspicious_instructions: Признак обнаружения хотя бы одной сигнатуры инъекции.
        matched_patterns: Кортеж идентификаторов сработавших сигнатур защиты.
    """

    cleaned_text: str
    has_suspicious_instructions: bool
    matched_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Фиксирует кортеж совпавших сигнатур."""
        object.__setattr__(self, "matched_patterns", tuple(self.matched_patterns))


def sanitize_control_characters(text: str, *, maximum_length: int = 4000) -> str:
    """Удаляет невидимые управляющие символы и ограничивает длину внешнего текста.

    Arguments:
        text: Исходный текст из внешнего источника или сообщения пользователя.
        maximum_length: Предельная длина итоговой строки.

    Returns:
        Очищенная и обрезанная строка без невидимых управляющих символов.
    """
    without_control = _CONTROL_CHARS_PATTERN.sub("", text)
    normalized = _MULTISPACE_PATTERN.sub(" ", without_control).strip()
    if len(normalized) > maximum_length:
        return normalized[:maximum_length].rstrip()
    return normalized


def inspect_untrusted_text(text: str, *, maximum_length: int = 4000) -> SanitizationInspectionResult:
    """Проверяет текст на наличие попыток перехвата инструкций и утечки системного промпта.

    Сначала удаляет невидимые символы (включая zero-width пробелы), чтобы исключить
    обход детектора разбивкой слов.

    Arguments:
        text: Проверяемый текст события или входящего сообщения.
        maximum_length: Максимальная длина очищенного текста.

    Returns:
        Результат проверки с флагом угрозы и перечнем сработавших сигнатур.
    """
    cleaned_text = sanitize_control_characters(text, maximum_length=maximum_length)
    matched: list[str] = [
        pattern_name for pattern_name, regex in _INJECTION_PATTERNS.items() if regex.search(cleaned_text) is not None
    ]
    return SanitizationInspectionResult(
        cleaned_text=cleaned_text, has_suspicious_instructions=bool(matched), matched_patterns=tuple(matched)
    )


def wrap_untrusted_event_data(text: str, *, maximum_length: int = 2000) -> str:
    """Безопасно изолирует текст карточки события в теги `<untrusted_event_data>`.

    Нейтрализует вложенные закрывающие теги внутри самого текста, предотвращая
    выход недоверенных данных за пределы блока контекста.

    Arguments:
        text: Сырое описание или заголовок внешнего мероприятия.
        maximum_length: Максимальная длина содержимого.

    Returns:
        Строка, обрамлённая тегами `<untrusted_event_data>...</untrusted_event_data>`.
    """
    cleaned = sanitize_control_characters(text, maximum_length=maximum_length)
    neutralized = _UNTRUSTED_TAG_PATTERN.sub("[удалённый_тег]", cleaned)
    return f"<untrusted_event_data>\n{neutralized}\n</untrusted_event_data>"


def contains_system_prompt_leak(
    response_text: str, *, protected_markers: RealCollection[str] = DEFAULT_PROTECTED_PROMPT_MARKERS
) -> bool:
    """Проверяет текст ответа модели на случайную утечку системных маркеров и промпта.

    Arguments:
        response_text: Сгенерированный текст ответа модели.
        protected_markers: Набор защищённых фраз или маркеров системного промпта.

    Returns:
        `True`, если в ответе обнаружен хотя бы один защищённый маркер.
    """
    lowered_response = response_text.lower()
    return any(marker.lower() in lowered_response for marker in protected_markers if marker.strip())

"""Корневая иерархия исключений приложения и инфраструктурных категорий."""

from enum import Enum
from typing import Self, override

__all__ = [
    "AccessCredentialsInvalidError",
    "AccessEndpointInvalidError",
    "AccessException",
    "AccessPermissionDeniedError",
    "ApplicationException",
    "DomainException",
    "InfrastructureException",
    "LifecycleClientClosedError",
    "LifecycleClientNotInitializedError",
    "LifecycleException",
    "ModelContentBlockedError",
    "ModelContextLimitExceededError",
    "ModelException",
    "ModelNotFoundError",
    "ModelOutputInvalidError",
    "ModelOutputTruncatedError",
    "ModelRequestInvalidError",
    "NetworkConnectionFailureError",
    "NetworkConnectionTimeoutError",
    "NetworkDataTransferError",
    "NetworkException",
    "ServiceAvailabilityException",
    "ServiceRateLimitExceededError",
    "ServiceUnavailableError",
    "ValidationException",
    "ValidationInputError",
    "ValidationSchemaError",
]


class ApplicationException(Exception):
    """Корневое исключение для всех ошибок приложения.

    Attributes:
        message: Человекочитаемое описание ошибки.
    """

    def __init__(self, message: str) -> None:
        """Инициализирует базовое исключение приложения.

        Arguments:
            message: Сообщение об ошибке.
        """
        super().__init__(message)
        self.message: str = message

    @override
    def __str__(self) -> str:
        """Форматирует сообщение и публичный диагностический контекст экземпляра.

        Returns:
            Строковое представление ошибки с перечислением непустых атрибутов.
        """
        context_parts: list[str] = []
        for attribute_name, attribute_value in self.__dict__.items():
            if attribute_name == "message" or attribute_name.startswith("_") or attribute_value is None:
                continue
            formatted_value = attribute_value.value if isinstance(attribute_value, Enum) else attribute_value
            context_parts.append(f"{attribute_name}={formatted_value!r}")

        if not context_parts:
            return self.message
        return f"{self.message} ({', '.join(context_parts)})"

    @override
    def __reduce__(self) -> tuple[object, ...]:
        """Обеспечивает корректную сериализацию исключения через `pickle` и `deepcopy`.

        Returns:
            Кортеж восстановления состояния экземпляра.
        """
        return self.__class__._reconstruct, (self.__dict__.copy(),)

    @classmethod
    def _reconstruct(cls, state: dict[str, object]) -> Self:
        """Восстанавливает экземпляр исключения при десериализации.

        Arguments:
            state: Сохранённый словарь атрибутов экземпляра.

        Returns:
            Восстановленный экземпляр исключения.
        """
        instance = cls.__new__(cls)
        message = state.get("message", "")
        Exception.__init__(instance, message)
        instance.__dict__.update(state)
        return instance


class DomainException(ApplicationException):
    """Базовый класс исключений доменного и прикладного слоёв."""


class InfrastructureException(ApplicationException):
    """Базовый класс исключений инфраструктурного слоя."""


class AccessException(InfrastructureException):
    """Ошибки аутентификации, авторизации и разрешения сетевых адресов."""


class AccessCredentialsInvalidError(AccessException):
    """Некорректные или истекшие учётные данные внешнего сервиса."""


class AccessEndpointInvalidError(AccessException):
    """Некорректный адрес подключения или ошибка разрешения DNS."""


class AccessPermissionDeniedError(AccessException):
    """Недостаточно прав для выполнения операции во внешней системе."""


class LifecycleException(InfrastructureException):
    """Ошибки жизненного цикла подключений и клиентов."""


class LifecycleClientNotInitializedError(LifecycleException):
    """Обращение к неинициализированному инфраструктурному клиенту."""


class LifecycleClientClosedError(LifecycleException):
    """Обращение к уже закрытому инфраструктурному клиенту."""


class ModelException(InfrastructureException):
    """Ошибки вызова языковой модели и обработки её ответа."""


class ModelContentBlockedError(ModelException):
    """Запрос или ответ модели заблокирован фильтрами безопасности."""


class ModelContextLimitExceededError(ModelException):
    """Превышен лимит контекстного окна языковой модели."""


class ModelNotFoundError(ModelException):
    """Запрошенная языковая модель не найдена у провайдера."""


class ModelOutputInvalidError(ModelException):
    """Ответ языковой модели не соответствует ожидаемому структурированному контракту."""


class ModelOutputTruncatedError(ModelException):
    """Ответ языковой модели был оборван из-за ограничения длины генерации."""


class ModelRequestInvalidError(ModelException):
    """Некорректные параметры запроса к языковой модели."""


class NetworkException(InfrastructureException):
    """Транспортные ошибки сети, тайм-ауты и сбои передачи данных."""


class NetworkConnectionFailureError(NetworkException):
    """Не удалось установить сетевое соединение с внешним ресурсом."""


class NetworkConnectionTimeoutError(NetworkException):
    """Превышен тайм-аут ожидания ответа от внешнего ресурса."""


class NetworkDataTransferError(NetworkException):
    """Обрыв или повреждение потока данных при сетевом обмене."""


class ServiceAvailabilityException(InfrastructureException):
    """Ошибки доступности и квотирования внешних сервисов."""


class ServiceRateLimitExceededError(ServiceAvailabilityException):
    """Превышен лимит частоты запросов к внешнему сервису."""


class ServiceUnavailableError(ServiceAvailabilityException):
    """Внешний сервис временно недоступен (ошибка 5xx или технический перерыв)."""


class ValidationException(InfrastructureException):
    """Ошибки разбора или валидации форматов входных и внешних данных."""


class ValidationSchemaError(ValidationException):
    """Нарушение ожидаемой схемы или структуры данных."""


class ValidationInputError(ValidationException):
    """Некорректные входные данные или неподдерживаемый формат."""

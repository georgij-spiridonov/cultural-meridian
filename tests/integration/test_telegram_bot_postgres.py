"""Интеграционные тесты Telegram-бота поверх реальной базы данных PostgreSQL 17."""

import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import Any, cast, override

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from src.application import AdministrationService, EventImportService, EventSearchService, SubscriptionService
from src.core.configuration import (
    TelegramSettings,
    WebSettings,
    get_application_settings,
    get_database_settings,
    get_logging_settings,
)
from src.core.types import DialogChannel
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.entrypoints.bootstrap import ApplicationContainer
from src.entrypoints.command_line.seed import DEFAULT_VENUES_CATALOG_PATH, seed_reference_catalog
from src.entrypoints.telegram_bot.handlers import create_telegram_router
from src.entrypoints.telegram_bot.keyboards import EventCardCallback, SubscriptionCallback, compute_city_token
from src.infrastructure.database import (
    DatabaseEngineManager,
    PostgreSqlAdministrationRepository,
    PostgreSqlDialogRepository,
    PostgreSqlEventRepository,
    PostgreSqlRawEventRepository,
)

pytestmark = pytest.mark.integration


class _OfflineIntegrationTelegramSession(BaseSession):
    """Оффлайн-сессия `aiogram` для сквозной проверки обработчиков поверх PostgreSQL."""

    def __init__(self) -> None:
        """Инициализирует список исходящих методов Telegram API."""
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._next_message_identifier = 500

    @override
    async def close(self) -> None:
        """Заглушка закрытия сессии."""

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        """Сохраняет исходящий вызов и возвращает синтетическое сообщение."""
        del timeout
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self._next_message_identifier += 1
            chat_identifier = int(method.chat_id) if method.chat_id is not None else 5000
            return cast(
                TelegramType,
                Message(
                    message_id=self._next_message_identifier,
                    date=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
                    chat=Chat(id=chat_identifier, type="private"),
                    from_user=User(id=bot.id or 888888, is_bot=True, first_name="CulturalMeridianBot"),
                    text=method.text,
                ),
            )
        return cast(TelegramType, True)

    @override
    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        """Заглушка потоковой загрузки."""
        del url, headers, timeout, chunk_size, raise_for_status
        if False:
            yield b""


def _build_future_iso(days_offset: int, hour: int = 19) -> str:
    target = (datetime.now(tz=BASHKORTOSTAN_TIMEZONE) + timedelta(days=days_offset)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return target.isoformat()


@pytest_asyncio.fixture
async def telegram_container(database_engine_manager: DatabaseEngineManager) -> ApplicationContainer:
    """Создаёт `ApplicationContainer` с тестовой БД PostgreSQL 17 и импортирует тестовые события."""
    event_repository = PostgreSqlEventRepository(database_engine_manager)
    raw_event_repository = PostgreSqlRawEventRepository(database_engine_manager)
    dialog_repository = PostgreSqlDialogRepository(database_engine_manager)
    administration_repository = PostgreSqlAdministrationRepository(database_engine_manager)

    search_service = EventSearchService(event_repository=event_repository)
    import_service = EventImportService(
        event_repository=event_repository, administration_repository=administration_repository
    )
    subscription_service = SubscriptionService(
        event_repository=event_repository, administration_repository=administration_repository
    )
    administration_service = AdministrationService(
        event_repository=event_repository, administration_repository=administration_repository
    )

    container = ApplicationContainer(
        application_settings=get_application_settings(),
        database_settings=get_database_settings(),
        logging_settings=get_logging_settings(),
        web_settings=WebSettings(),
        telegram_settings=TelegramSettings(BOT_TOKEN="", PAGE_SIZE=2, PUBLIC_WEB_BASE_URL="https://afisha.example.org"),
        database_engine_manager=database_engine_manager,
        event_repository=event_repository,
        raw_event_repository=raw_event_repository,
        dialog_repository=dialog_repository,
        administration_repository=administration_repository,
        search_service=search_service,
        import_service=import_service,
        subscription_service=subscription_service,
        administration_service=administration_service,
    )

    await seed_reference_catalog(database_engine_manager, catalog_path=DEFAULT_VENUES_CATALOG_PATH)
    payload = json.dumps(
        {
            "events": [
                {
                    "external_identifier": "tg-pg-1",
                    "title": "Джазовый вечер в Стерлитамаке",
                    "category": "concert",
                    "date_start": _build_future_iso(2, 19),
                    "duration_minutes": 100,
                    "city": "Стерлитамак",
                    "venue": "СГТКО",
                    "address": "пр. Ленина, 30В",
                    "price_minimum": 500,
                    "price_maximum": 900,
                    "is_free": False,
                    "age_limit": 12,
                    "description": "Живое выступление джазового квинтета.",
                    "source_url": "https://example.org/events/tg-pg-1",
                    "source_name": "Афиша Стерлитамака",
                },
                {
                    "external_identifier": "tg-pg-2",
                    "title": "Бесплатная выставка в Уфе",
                    "category": "exhibition",
                    "date_start": _build_future_iso(3, 11),
                    "duration_minutes": 120,
                    "city": "Уфа",
                    "venue": "Башгосфилармония",
                    "address": "ул. Гоголя, 58",
                    "price_minimum": 0,
                    "price_maximum": 0,
                    "is_free": True,
                    "age_limit": 0,
                    "description": "Экспозиция народного искусства.",
                    "source_url": "https://example.org/events/tg-pg-2",
                    "source_name": "Музеи Уфы",
                },
            ]
        },
        ensure_ascii=False,
    ).encode("utf-8")
    await import_service.import_events(
        raw_content=payload, file_format="json", filename="telegram_seed.json", actor="telegram-test-seed"
    )
    return container


async def test_telegram_bot_end_to_end_with_postgresql(telegram_container: ApplicationContainer) -> None:
    """Проверяет поиск с опечаткой, историю диалога, карточку, подписку и массовую отписку в PostgreSQL."""
    dispatcher = Dispatcher()
    dispatcher.include_router(create_telegram_router(telegram_container, duplicate_callback_cooldown_seconds=0.0))
    session = _OfflineIntegrationTelegramSession()
    bot = Bot(
        token="123456:INTEGRATION_TEST_TOKEN", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    user_identifier = 550011
    search_update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
            chat=Chat(id=user_identifier, type="private"),
            from_user=User(id=user_identifier, is_bot=False, first_name="Айрат"),
            text="Концерт в стерлитакмае",
        ),
    )
    await dispatcher.feed_update(bot, search_update)

    search_reply = session.requests[-1]
    assert isinstance(search_reply, SendMessage)
    assert "Джазовый вечер в Стерлитамаке" in search_reply.text

    session_state = await telegram_container.dialog_repository.get_session_state(
        session_identifier=f"telegram:{user_identifier}",
        channel=DialogChannel.TELEGRAM,
        subscriber_identifier=str(user_identifier),
    )
    assert len(session_state.recent_messages) == 2
    assert session_state.recent_messages[0].content == "Концерт в стерлитакмае"

    assert isinstance(search_reply.reply_markup, InlineKeyboardMarkup)
    event_callback_data = next(
        button.callback_data
        for row in search_reply.reply_markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("event:")
    )
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="pg-cb-card",
                from_user=User(id=user_identifier, is_bot=False, first_name="Айрат"),
                chat_instance="chat-pg",
                message=Message(
                    message_id=501,
                    date=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
                    chat=Chat(id=user_identifier, type="private"),
                    from_user=User(id=888888, is_bot=True, first_name="CulturalMeridianBot"),
                    text="Список",
                ),
                data=event_callback_data,
            ),
        ),
    )
    card_reply = session.requests[-2]
    assert isinstance(card_reply, EditMessageText)
    assert card_reply.text is not None
    assert "Афиша Стерлитамака" in card_reply.text
    assert isinstance(card_reply.reply_markup, InlineKeyboardMarkup)
    event_identifier_str = EventCardCallback.unpack(event_callback_data).event_identifier
    assert any(
        button.url == f"https://afisha.example.org/events/{event_identifier_str}"
        for row in card_reply.reply_markup.inline_keyboard
        for button in row
        if button.url
    )

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=3,
            callback_query=CallbackQuery(
                id="pg-cb-sub-create",
                from_user=User(id=user_identifier, is_bot=False, first_name="Айрат"),
                chat_instance="chat-pg",
                message=Message(
                    message_id=501,
                    date=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
                    chat=Chat(id=user_identifier, type="private"),
                    from_user=User(id=888888, is_bot=True, first_name="CulturalMeridianBot"),
                    text="Подписки",
                ),
                data=SubscriptionCallback(
                    action_name="create", first_argument=compute_city_token("Уфа"), second_argument="exhibition"
                ).pack(),
            ),
        ),
    )
    subscriptions = await telegram_container.subscription_service.list_subscriptions(
        channel=DialogChannel.TELEGRAM, subscriber_identifier=str(user_identifier)
    )
    assert len(subscriptions) == 1
    assert subscriptions[0].city == "Уфа"

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=4,
            message=Message(
                message_id=2,
                date=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
                chat=Chat(id=user_identifier, type="private"),
                from_user=User(id=user_identifier, is_bot=False, first_name="Айрат"),
                text="/unsubscribe all",
            ),
        ),
    )
    confirm_reply = session.requests[-1]
    assert isinstance(confirm_reply, SendMessage)
    assert isinstance(confirm_reply.reply_markup, InlineKeyboardMarkup)
    confirm_data = next(
        button.callback_data
        for row in confirm_reply.reply_markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("pa:confirm:")
    )

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=5,
            callback_query=CallbackQuery(
                id="pg-cb-confirm-unsub",
                from_user=User(id=user_identifier, is_bot=False, first_name="Айрат"),
                chat_instance="chat-pg",
                message=Message(
                    message_id=505,
                    date=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
                    chat=Chat(id=user_identifier, type="private"),
                    from_user=User(id=888888, is_bot=True, first_name="CulturalMeridianBot"),
                    text="Подтверждение",
                ),
                data=confirm_data,
            ),
        ),
    )
    remaining_subscriptions = await telegram_container.subscription_service.list_subscriptions(
        channel=DialogChannel.TELEGRAM, subscriber_identifier=str(user_identifier)
    )
    assert remaining_subscriptions == ()

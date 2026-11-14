"""Интеграционные тесты планировщика «Выходной день» и геолокации поверх PostgreSQL 17."""

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import date, datetime, time
from typing import Any, cast, override

import httpx
import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from src.application import WeekendPlannerService
from src.core.configuration import (
    DatabaseSettings,
    GeolocationSettings,
    LanguageModelSettings,
    TelegramSettings,
    WebSettings,
)
from src.core.types import DialogChannel
from src.domain.entities import WeekendPlanRequest
from src.domain.services import BASHKORTOSTAN_TIMEZONE
from src.entrypoints.bootstrap import ApplicationContainer, build_application_container
from src.entrypoints.command_line.seed import (
    DEFAULT_SEED_EVENTS_PATH,
    DEFAULT_VENUES_CATALOG_PATH,
    seed_events_from_file,
    seed_reference_catalog,
)
from src.entrypoints.telegram_bot.handlers import create_telegram_router
from src.entrypoints.web import create_web_application
from src.infrastructure.database import DatabaseEngineManager
from src.infrastructure.geolocation import HybridGeolocationResolver

pytestmark = pytest.mark.integration


class _OfflinePlannerTelegramSession(BaseSession):
    """Оффлайн-сессия `aiogram` для интеграционного тестирования сценария `/plan`."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._next_message_identifier = 700

    @override
    async def close(self) -> None:
        """Заглушка закрытия сессии."""

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        """Сохраняет исходящий запрос Telegram API и возвращает синтетическое сообщение."""
        del timeout
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self._next_message_identifier += 1
            chat_identifier = int(method.chat_id) if method.chat_id is not None else 7000
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


@pytest_asyncio.fixture
async def planner_container(
    test_database_settings: DatabaseSettings, database_engine_manager: DatabaseEngineManager
) -> AsyncIterator[ApplicationContainer]:
    """Собирает `build_application_container()` поверх тестовой БД PostgreSQL 17 и сидирует каталог."""
    del database_engine_manager
    container = build_application_container(
        database_settings=test_database_settings,
        language_model_settings=LanguageModelSettings(_env_file=None, API_KEY=""),
        geolocation_settings=GeolocationSettings(
            _env_file=None, EXTERNAL_ENABLED=False, VENUES_CATALOG_PATH=DEFAULT_VENUES_CATALOG_PATH
        ),
        telegram_settings=TelegramSettings(BOT_TOKEN="", PAGE_SIZE=5, PUBLIC_WEB_BASE_URL="https://afisha.example.org"),
        web_settings=WebSettings(
            ADMIN_USERNAME="planner_mod",
            ADMIN_PASSWORD="planner_secret_password",
            SESSION_SECRET="planner-integration-session-secret",
            RATE_LIMIT_REQUESTS_PER_MINUTE=500,
            FEEDBACK_RATE_LIMIT_PER_MINUTE=20,
        ),
    )
    await container.startup()
    try:
        await seed_reference_catalog(container.database_engine_manager, catalog_path=DEFAULT_VENUES_CATALOG_PATH)
        await seed_events_from_file(container.event_repository, events_path=DEFAULT_SEED_EVENTS_PATH)
        yield container
    finally:
        await container.shutdown()


async def test_build_application_container_weekend_planner_service_postgres(
    planner_container: ApplicationContainer,
) -> None:
    """Проверяет сборку `build_application_container()` и подбор маршрута из 2–4 событий в PostgreSQL."""
    assert isinstance(planner_container.geolocation_resolver, HybridGeolocationResolver)
    assert isinstance(planner_container.weekend_planner, WeekendPlannerService)

    result = await planner_container.weekend_planner.plan_weekend(
        WeekendPlanRequest(
            city="Уфа",
            target_date=date(2026, 11, 15),
            window_start_time=time(10, 0),
            window_end_time=time(21, 0),
            budget_rubles=2000,
            companions_count=1,
            child_age=None,
            maximum_travel_minutes=20,
        )
    )

    assert result.is_feasible is True
    assert 2 <= len(result.steps) <= 4
    assert len(result.steps) == 3
    assert result.total_travel_minutes > 0
    assert result.steps[0].travel_from_previous_minutes == 0
    assert all(step.travel_from_previous_minutes > 0 for step in result.steps[1:])
    assert result.total_minimum_cost_rubles == 1000

    for previous_step, next_step in zip(result.steps, result.steps[1:], strict=False):
        assert previous_step.ends_at <= next_step.starts_at


async def test_web_weekend_plan_api_ssr_and_htmx_postgres(planner_container: ApplicationContainer) -> None:
    """Проверяет `POST /api/v1/weekend-plan`, `GET /planner` и HTMX `/partials/weekend-plan` поверх PostgreSQL."""
    application = create_web_application(container=planner_container)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Валидный JSON API запрос -> 200 OK
        valid_response = await client.post(
            "/api/v1/weekend-plan",
            json={
                "city": "Уфа",
                "target_date": "2026-11-15",
                "window_start_time": "10:00:00",
                "window_end_time": "21:00:00",
                "budget_rubles": 2000,
                "companions_count": 1,
                "maximum_travel_minutes": 20,
            },
        )
        assert valid_response.status_code == 200
        valid_payload = valid_response.json()
        assert valid_payload["is_feasible"] is True
        assert 2 <= len(valid_payload["steps"]) <= 4
        assert valid_payload["total_travel_minutes"] > 0
        assert all(step["event_path"].startswith("/events/") for step in valid_payload["steps"])

        # 2. Невалидное окно -> 422
        invalid_response = await client.post(
            "/api/v1/weekend-plan",
            json={
                "city": "Уфа",
                "target_date": "2026-11-15",
                "window_start_time": "18:00:00",
                "window_end_time": "12:00:00",
                "budget_rubles": 2000,
                "companions_count": 1,
                "maximum_travel_minutes": 20,
            },
        )
        assert invalid_response.status_code == 422
        assert invalid_response.json()["code"] == "invalid_weekend_plan_request_error"

        # 3. Невозможный бюджет -> 200 OK с is_feasible=false и relaxation_suggestions
        infeasible_response = await client.post(
            "/api/v1/weekend-plan",
            json={
                "city": "Уфа",
                "target_date": "2026-11-15",
                "window_start_time": "10:00:00",
                "window_end_time": "21:00:00",
                "budget_rubles": 50,
                "companions_count": 1,
                "maximum_travel_minutes": 20,
            },
        )
        assert infeasible_response.status_code == 200
        infeasible_payload = infeasible_response.json()
        assert infeasible_payload["is_feasible"] is False
        assert infeasible_payload["failure_reason"]
        assert any(item["constraint_name"] == "budget" for item in infeasible_payload["relaxation_suggestions"])

        # 4. SSR страница GET /planner (без параметров и с параметрами)
        empty_planner_page = await client.get("/planner")
        assert empty_planner_page.status_code == 200
        assert "Выходной день" in empty_planner_page.text

        filled_planner_page = await client.get(
            "/planner",
            params={
                "city": "Уфа",
                "target_date": "2026-11-15",
                "window_start_time": "10:00",
                "window_end_time": "21:00",
                "budget_rubles": "2000",
                "companions_count": "1",
                "maximum_travel_minutes": "20",
            },
        )
        assert filled_planner_page.status_code == 200
        assert "Михаил Нестеров" in filled_planner_page.text
        assert "Золото сарматов" in filled_planner_page.text

        # 5. HTMX partial GET/POST /partials/weekend-plan
        htmx_get = await client.get(
            "/partials/weekend-plan",
            params={
                "city": "Уфа",
                "target_date": "2026-11-15",
                "window_start_time": "10:00",
                "window_end_time": "21:00",
                "budget_rubles": "50",
                "companions_count": "1",
                "maximum_travel_minutes": "20",
            },
            headers={"HX-Request": "true", "HX-Target": "planner-results"},
        )
        assert htmx_get.status_code == 200
        assert htmx_get.headers.get("HX-Push-Url", "").startswith("/planner?")
        assert "Увеличить бюджет" in htmx_get.text

        htmx_post = await client.post(
            "/partials/weekend-plan",
            data={
                "city": "Уфа",
                "target_date": "2026-11-15",
                "window_start_time": "10:00",
                "window_end_time": "21:00",
                "budget_rubles": "2000",
                "companions_count": "1",
                "maximum_travel_minutes": "20",
            },
            headers={"HX-Request": "true", "HX-Target": "planner-results"},
        )
        assert htmx_post.status_code == 200
        assert "Мелодии курая" in htmx_post.text


async def test_telegram_bot_weekend_planner_postgres(planner_container: ApplicationContainer) -> None:
    """Проверяет сквозной сценарий `/plan` и ослабление ограничений в Telegram-боте поверх PostgreSQL."""
    dispatcher = Dispatcher()
    dispatcher.include_router(create_telegram_router(planner_container, duplicate_callback_cooldown_seconds=0.0))
    session = _OfflinePlannerTelegramSession()
    bot = Bot(
        token="123456:INTEGRATION_PLANNER_TOKEN",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    user_identifier = 770022
    # 1. Невозможный бюджет -> подсказка ослабления `wp:relax:*:budget`
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
                chat=Chat(id=user_identifier, type="private"),
                from_user=User(id=user_identifier, is_bot=False, first_name="Тимур"),
                text="/plan Уфа 50 2026-11-15",
            ),
        ),
    )
    infeasible_reply = session.requests[-1]
    assert isinstance(infeasible_reply, SendMessage)
    assert isinstance(infeasible_reply.reply_markup, InlineKeyboardMarkup)
    relax_budget_cb = next(
        button.callback_data
        for row in infeasible_reply.reply_markup.inline_keyboard
        for button in row
        if button.callback_data
        and button.callback_data.startswith("wp:relax:")
        and button.callback_data.endswith(":budget")
    )

    # 2. Нажатие на кнопку ослабления бюджета перестраивает маршрут из PostgreSQL
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="pg-plan-relax-budget",
                from_user=User(id=user_identifier, is_bot=False, first_name="Тимур"),
                chat_instance="chat-pg-plan",
                message=Message(
                    message_id=701,
                    date=datetime.now(tz=BASHKORTOSTAN_TIMEZONE),
                    chat=Chat(id=user_identifier, type="private"),
                    from_user=User(id=888888, is_bot=True, first_name="CulturalMeridianBot"),
                    text="Ограничения",
                ),
                data=relax_budget_cb,
            ),
        ),
    )
    feasible_reply = session.requests[-2]
    assert isinstance(feasible_reply, EditMessageText)
    assert feasible_reply.text is not None
    assert "Михаил Нестеров" in feasible_reply.text
    assert "Золото сарматов" in feasible_reply.text
    assert "Мелодии курая" in feasible_reply.text

    session_state = await planner_container.dialog_repository.get_session_state(
        session_identifier=f"telegram:{user_identifier}",
        channel=DialogChannel.TELEGRAM,
        subscriber_identifier=str(user_identifier),
    )
    assert len(session_state.recent_messages) == 4

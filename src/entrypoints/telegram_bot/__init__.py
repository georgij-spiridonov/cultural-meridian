"""Пакет Telegram-бота «Культурный Меридиан» на базе `aiogram` v3."""

from src.entrypoints.telegram_bot.handlers import (
    TelegramBotDependencies,
    TelegramUpdateMiddleware,
    create_telegram_router,
)
from src.entrypoints.telegram_bot.runner import (
    build_bot_commands,
    build_telegram_dispatcher,
    main,
    run_idle_wait_loop,
    run_telegram_bot,
)

__all__ = [
    "TelegramBotDependencies",
    "TelegramUpdateMiddleware",
    "build_bot_commands",
    "build_telegram_dispatcher",
    "create_telegram_router",
    "main",
    "run_idle_wait_loop",
    "run_telegram_bot",
]

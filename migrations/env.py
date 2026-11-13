"""Среда выполнения миграций Alembic для асинхронного PostgreSQL 17."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.core.configuration import get_database_settings
from src.infrastructure.database.tables import DatabaseBase

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = DatabaseBase.metadata


def _resolve_database_url() -> str:
    """Возвращает строку подключения к PostgreSQL из настроек приложения."""
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return configured_url
    return get_database_settings().connection_url


def run_migrations_offline() -> None:
    """Генерирует SQL-скрипт миграций в автономном режиме без подключения к БД."""
    context.configure(
        url=_resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Выполняет миграции в рамках предоставленного синхронного соединения SQLAlchemy.

    Arguments:
        connection: Активное соединение SQLAlchemy, переданное из `AsyncConnection.run_sync`.
    """
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True, compare_server_default=True
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Создаёт асинхронный движок без пула и применяет миграции к PostgreSQL."""
    configuration_section = config.get_section(config.config_ini_section, {})
    configuration_section["sqlalchemy.url"] = _resolve_database_url()

    connectable = async_engine_from_config(configuration_section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Запускает применение миграций в онлайн-режиме."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

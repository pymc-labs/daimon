"""Async Alembic env. Reads DSN from DAIMON_DATABASE_URL or DAIMON_DATABASE__URL env var."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from daimon.core._models import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

db_url = os.environ.get("DAIMON_DATABASE_URL") or os.environ.get("DAIMON_DATABASE__URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)
elif not config.get_main_option("sqlalchemy.url"):
    # Flat DAIMON_DATABASE_URL is intentional — alembic CLI runs outside
    # pydantic-settings. The app uses nested DAIMON_DATABASE__URL.
    raise RuntimeError(
        "No database URL configured. Set DAIMON_DATABASE__URL "
        "(or DAIMON_DATABASE_URL for backwards compat) or sqlalchemy.url in alembic.ini."
    )

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

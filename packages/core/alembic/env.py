"""Async Alembic env; DSN from DAIMON_DATABASE_URL or DAIMON_DATABASE__URL.

Invariant: main revisions receive no repair writes. Only online upgrade/downgrade
may atomically repair a stored legacy preview stamp in the migration transaction.
Other commands never repair or perform implicit writes. Explicit stamp and
ensure_version bookkeeping retain normal Alembic behavior on non-legacy states.
Inspection never creates a version table, even on an empty/unversioned database.
Plain history/heads remain graph-only. Commands entering this env diagnose stored
legacy stamps without repairing; old literal targets and
offline legacy ranges are unsupported: run ``alembic upgrade head`` online first.

The deployer serializes migrations; this environment adds no advisory lock or
retry loop. deploy/lanes/README.md "Migration serialization" names the callers.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterable
from typing import cast

from alembic import context, util
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext, MigrationStep
from alembic.script import ScriptDirectory
from daimon.core._models import Base
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

db_url = os.environ.get("DAIMON_DATABASE_URL") or os.environ.get("DAIMON_DATABASE__URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)
elif not config.get_main_option("sqlalchemy.url") and "connection" not in config.attributes:
    # Flat DAIMON_DATABASE_URL is intentional — alembic CLI runs outside
    # pydantic-settings. The app uses nested DAIMON_DATABASE__URL.
    raise RuntimeError(
        "No database URL configured. Set DAIMON_DATABASE__URL "
        "(or DAIMON_DATABASE_URL for backwards compat) or sqlalchemy.url in alembic.ini."
    )

target_metadata = Base.metadata

LEGACY_REVISIONS = {
    "0111_cold_dm_agent": "0134_cold_dm_agent",
    "0112_routine_dm_destination": "0135_routine_dm_destination",
    "0113_unattended_turn_provenance": "0136_unattended_turn_provenance",
    "0114_turn_attendance": "0137_turn_attendance",
    "0115_origin_session_grants": "0138_origin_session_grants",
    "0116_thread_model_settings": "0139_thread_model_settings",
    "0117_clear_legacy_routine_output": "0140_clear_legacy_routine_output",
    "0118_lead_intake": "0141_lead_intake",
    "0119_watches": "0142_watches",
    "0120_watch_polling": "0143_watch_polling",
    "0121_routine_sandbox_tools": "0144_routine_sandbox_tools",
    "0122_recruitment": "0145_recruitment",
    "0110_cold_dm_agent": "0134_cold_dm_agent",
    "0111_routine_dm_destination": "0135_routine_dm_destination",
    "0112_unattended_turn_provenance": "0136_unattended_turn_provenance",
    "0113_microsoft_accounts": "0136_unattended_turn_provenance",
}


def _legacy_error(revision: str) -> util.CommandError:
    return util.CommandError(
        f"Legacy preview revision '{revision}' cannot be used for this command. "
        "Run `alembic upgrade head` online to reconcile the disposable preview database first. "
        "Only online upgrade/downgrade from a stored legacy stamp is supported; "
        "use canonical revision IDs for targets and offline SQL."
    )


def _reject_legacy_targets() -> None:
    opts = context.get_context().opts
    for option in ("starting_rev", "destination_rev"):
        value = str(opts.get(option, ""))
        for revision in LEGACY_REVISIONS:
            if revision in value:
                raise _legacy_error(revision)


def _is_migration_command() -> bool:
    # destination_rev is also set by stamp and autogenerate. Recognize the
    # actual Alembic command callbacks instead, for both CLI and command API.
    fn = context.get_context().opts.get("fn")
    return getattr(fn, "__module__", None) == "alembic.command" and getattr(
        fn, "__name__", None
    ) in ("upgrade", "downgrade")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    _reject_legacy_targets()
    with context.begin_transaction():
        context.run_migrations()


def _reject_stored_legacy(revisions: tuple[str, ...]) -> None:
    for revision in revisions:
        if revision in LEGACY_REVISIONS:
            raise _legacy_error(revision)


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    migrating = _is_migration_command()
    fn = context.get_context().opts.get("fn")
    bookkeeping = getattr(fn, "__module__", None) == "alembic.command" and getattr(
        fn, "__name__", None
    ) in ("do_stamp", "do_ensure_version")
    if bookkeeping:
        original = cast(Callable[[tuple[str, ...], MigrationContext], Iterable[MigrationStep]], fn)

        def guard_bookkeeping(
            revisions: tuple[str, ...], migration_context: MigrationContext
        ) -> Iterable[MigrationStep]:
            # Reuse Alembic's normal head read: non-legacy bookkeeping emits
            # exactly the same SQL as main, including version-table creation.
            _reject_stored_legacy(revisions)
            return original(revisions, migration_context)

        context.configure(
            connection=connection, target_metadata=target_metadata, fn=guard_bookkeeping
        )
    else:
        _reject_legacy_targets()
        if not migrating:
            context.configure(
                connection=connection, target_metadata=target_metadata, dont_mutate=True
            )
    with context.begin_transaction():
        if migrating:
            _normalize_preview_revision(connection)
        elif not bookkeeping or context.get_context().opts.get("purge"):
            # Purge deletes rows BEFORE invoking its callback, so validate the
            # stored stamp first. Inspection likewise diagnoses before lookup.
            _reject_stored_legacy(context.get_context().get_current_heads())
        context.run_migrations()


def _normalize_preview_revision(connection: Connection) -> None:
    """Install missing main DDL and translate a historical stamp in one transaction.

    Preview DDL already represented by the stored stamp must never be replayed.
    The usage index is built without CONCURRENTLY here because its canonical
    migration's autocommit block would commit a partially completed repair.
    """
    migration_context = context.get_context()
    revisions = migration_context.get_current_heads()
    legacy = [revision for revision in revisions if revision in LEGACY_REVISIONS]
    if not legacy:
        return
    if len(revisions) != 1:
        raise util.CommandError(
            "Cannot repair a multi-head legacy preview state; expected one stamp."
        )
    old_revision = legacy[0]
    scripts = ScriptDirectory.from_config(config)
    predecessors = [
        "0111_turn_origin_foreign_sender",
        "0112_selfhosted_resources",
        "0113_selfhosted_skill_catalog",
        "0114_selfhosted_skill_pins",
        "0115_progress_plan_scopes",
        "0116_shared_channel_facts",
        "0117_agent_channel_memory_store",
        "0118_agent_backend_model",
        "0119_agent_owner_settings",
        "0120_mcp_token_surface",
        "0121_refused_turn_outcome",
        "0122_thread_provider_release",
        "0123_tenant_backends_explicit",
        "0128_fleet_run_end_notice",
    ]
    if old_revision in {
        "0110_cold_dm_agent",
        "0111_routine_dm_destination",
        "0112_unattended_turn_provenance",
    }:
        predecessors.insert(0, "0110_microsoft_accounts")
    with Operations.context(migration_context):
        for revision in predecessors:
            script = scripts.get_revision(revision)
            assert script is not None
            script.module.upgrade()
        # Match 0131's index and invalid-index retry semantics, atomically.
        invalid = connection.scalar(
            text(
                "SELECT NOT indisvalid FROM pg_index "
                "WHERE indexrelid = to_regclass('ix_usage_observation_tenant_id')"
            )
        )
        if invalid:
            connection.execute(text("DROP INDEX ix_usage_observation_tenant_id"))
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_usage_observation_tenant_id "
                "ON usage_observation (tenant_id)"
            )
        )
    result = connection.execute(
        text("UPDATE alembic_version SET version_num = :new WHERE version_num = :old"),
        {"new": LEGACY_REVISIONS[old_revision], "old": old_revision},
    )
    if result.rowcount != 1:
        raise util.CommandError("Legacy preview repair expected exactly one version row.")


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Migrations re-encrypt agent keys; keep bound values out of SQL errors.
        hide_parameters=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif "connection" in config.attributes:
    do_run_migrations(config.attributes["connection"])
else:
    asyncio.run(run_migrations_online())

"""Exercise deployed main/preview stamps through real Alembic and PostgreSQL.

The historical scripts reproduce the preview chain as it stood at 7df20d82c,
before the ID repair: the canonical migrations under their old revision IDs
and parents. Building
those historical schemas proves compatibility without imitating them by merely
stamping a database that already has the new schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from alembic import command, util
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from daimon.core._models import Base
from daimon.testing.db import DatabasePreflight, build_test_engine, require_owned_sessionmaker
from sqlalchemy import Connection, Text, event, inspect, text
from sqlalchemy.engine import ExecutionContext, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "packages/core/alembic"
FORK = "0109_github_tokens_live_index"
MAIN = "0110_microsoft_accounts"
MAIN_HEAD = "0131_usage_obs_tenant_idx"
MAIN_SUCCESSORS = [
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
    MAIN_HEAD,
]
PREVIEW_IDS = {
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
}
FROZEN_PREVIEW = Path(__file__).parent / "fixtures/preview_migrations"
PREVIOUS_HEAD = "0140_clear_legacy_routine_output"
LEAD_HEAD = "0141_lead_intake"
WATCH_HEAD = "0142_watches"
WATCH_POLLING = "0143_watch_polling"
SANDBOX_HEAD = "0144_routine_sandbox_tools"
HEAD = "0145_recruitment"
CHAIN = [
    MAIN,
    *MAIN_SUCCESSORS,
    "0134_cold_dm_agent",
    "0135_routine_dm_destination",
    "0136_unattended_turn_provenance",
    "0137_turn_attendance",
    "0138_origin_session_grants",
    "0139_thread_model_settings",
    PREVIOUS_HEAD,
    LEAD_HEAD,
    WATCH_HEAD,
    WATCH_POLLING,
    SANDBOX_HEAD,
    HEAD,
]
# Added by SANDBOX_HEAD, so absent from every older revision the chain is checked at.
SANDBOX_COLUMNS = {
    "sandbox_tools_enabled",
    "sandbox_tools_set_by",
    "sandbox_tools_set_at",
    "sandbox_agent_id",
    "sandbox_agent_version",
    "sandbox_environment_id",
    "sandbox_environment_updated_at",
}
# The preview chain before the ID repair: each legacy revision ran the
# canonical script under its own ID and parent. The sha256 pins the bytes
# preview databases ran, so an edit to a deployed canonical script fails here
# instead of silently changing the historical chain.
PREVIEW_CHAIN = {
    "0110_cold_dm_agent": (
        "0134_cold_dm_agent",
        FORK,
        "0dd1c622c56c93b5a37aeebd0f1dff4bbdf369f900c84bc82c0eea409b6f32d8",
    ),
    "0111_routine_dm_destination": (
        "0135_routine_dm_destination",
        "0110_cold_dm_agent",
        "c6847980363d5509ef1e6d0181796a1c41dd564b3ed304235bff7e36a29cf318",
    ),
    "0112_unattended_turn_provenance": (
        "0136_unattended_turn_provenance",
        "0111_routine_dm_destination",
        "c5738445743c6b1d854199720f01b99c341169395a8072633b07bff193f89fe6",
    ),
    "0113_microsoft_accounts": (
        MAIN,
        "0112_unattended_turn_provenance",
        "5c531e579a7475a174797ce30fe031e716fc7917aa6e0359de74d416405a50b9",
    ),
}
TABLES = {
    "selfhosted_agents",
    "selfhosted_environments",
    "selfhosted_resource_operations",
    "progress_plan_scopes",
    "thread_model_settings",
    "shared_channels",
    "agent_channel_memory_store",
    "agent_owner",
    "agent_backend_revision",
    "mcp_tokens",
    "turn_origins",
    "platform_principals",
    "tenant_access_policies",
    "usage_observation",
    "microsoft_user_tokens",
    "microsoft_sign_ins",
    "direct_message_policies",
    "routines",
    "lead_intake_configs",
    "lead_intakes",
    "lead_intake_effects",
    "lead_resolution_audit",
    "lead_mailbox_states",
    "lead_intake_executions",
    "lead_opportunities",
    "lead_mailbox_scans",
    "lead_mailbox_messages",
    # Added by SANDBOX_HEAD, with its index and via check; compared from there on.
    "routine_sandbox_history",
    "recruitment_threads",
    "recruitment_messages",
    "recruitment_outbox",
}


def _config(scripts: Path, connection: Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(scripts))
    config.set_main_option(
        "sqlalchemy.url",
        connection.engine.url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    config.attributes["connection"] = connection
    return config


def _historical_scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "historical"
    versions = scripts / "versions"
    versions.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fixtures/preview_alembic_env.py", scripts / "env.py")
    for source in (SCRIPTS / "versions").glob("*.py"):
        if source.stem not in CHAIN:
            shutil.copy(source, versions / source.name)
    for legacy, (canonical, parent, digest) in PREVIEW_CHAIN.items():
        frozen = next((old for old, new in PREVIEW_IDS.items() if new == canonical), canonical)
        script = (
            FROZEN_PREVIEW / f"{frozen}.py"
            if canonical != MAIN
            else SCRIPTS / "versions" / f"{canonical}.py"
        ).read_text()
        script, revisions = re.subn(
            r'^revision: str = ".*"$', f'revision: str = "{legacy}"', script, flags=re.M
        )
        script, parents = re.subn(
            r'^down_revision: str \| None = ".*"$',
            f'down_revision: str | None = "{parent}"',
            script,
            flags=re.M,
        )
        assert revisions == parents == 1
        assert hashlib.sha256(script.encode()).hexdigest() == digest, (
            f"{canonical} no longer matches what preview ran as {legacy}"
        )
        (versions / f"{legacy}.py").write_text(script)
    return scripts


def _assert_head(connection: Connection, expected: str = HEAD) -> None:
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == expected

    compared_tables = (
        TABLES
        if expected in (LEAD_HEAD, WATCH_HEAD, WATCH_POLLING, SANDBOX_HEAD, HEAD)
        else {name for name in TABLES if not name.startswith("lead_")}
    )
    if expected != HEAD:
        compared_tables = {name for name in compared_tables if not name.startswith("recruitment_")}
    if expected not in (SANDBOX_HEAD, HEAD):
        compared_tables = compared_tables - {"routine_sandbox_history"}

    def include_name(name: str | None, kind: str, _parents: dict[str, str | None]) -> bool:
        return kind != "table" or name in compared_tables

    def include_object(
        obj: object,
        name: str | None,
        kind: str,
        reflected: bool,
        _compare_to: object,
    ) -> bool:
        # Historical main retains these two objects for older readers, while
        # its ORM intentionally omits them. Fresh/repaired schema parity below
        # still compares them, including the retained index's definition.
        if reflected and kind == "index" and name == "mcp_tokens_account_idx":
            return False
        if reflected and kind == "column" and name == "is_external":
            return getattr(getattr(obj, "table", None), "name", None) != "turn_origins"
        if kind == "column" and expected not in (SANDBOX_HEAD, HEAD):
            return name not in SANDBOX_COLUMNS
        return kind != "table" or name in compared_tables

    migration_context = MigrationContext.configure(
        connection, opts={"include_name": include_name, "include_object": include_object}
    )
    assert not compare_metadata(migration_context, Base.metadata)
    inspector = inspect(connection)
    constraints = {c["name"] for c in inspector.get_check_constraints("routines")}
    assert {"ck_routines_destination_kind", "ck_routines_dm_owner"} <= constraints
    if expected in (SANDBOX_HEAD, HEAD):
        # compare_metadata does not compare CHECK constraints.
        [via] = [
            c
            for c in inspector.get_check_constraints("routine_sandbox_history")
            if c["name"] == "ck_routine_sandbox_history_via"
        ]
        assert all(f"'{name}'" in via["sqltext"] for name in ("mcp", "cli", "edit"))
    # The usage table retains legacy repair columns intentionally absent from
    # the ORM. Compare only the two columns introduced by this chain.
    usage_columns = {c["name"]: c for c in inspector.get_columns("usage_reconcile_progress")}
    for name in ("unattended_turn_event_id", "unattended_rules_channel_id"):
        assert isinstance(usage_columns[name]["type"], Text)
        assert usage_columns[name]["nullable"]
    connection.commit()


def test_authoritative_chain() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [HEAD]
    parent = FORK
    for revision in CHAIN:
        node = script.get_revision(revision)
        assert node is not None and node.down_revision == parent
        assert Path(node.path).stem == revision
        parent = revision


def test_historical_scripts_match_what_preview_ran(tmp_path: Path) -> None:
    versions = _historical_scripts(tmp_path) / "versions"
    assert {p.stem for p in versions.glob("*.py")} >= set(PREVIEW_CHAIN)


@pytest.fixture
async def migration_database(db_preflight: DatabasePreflight) -> AsyncIterator[AsyncEngine]:
    """An owned empty database, independent of the ORM-based shared fixtures."""
    database = f"daimon_test_nc_migration_{os.getpid()}_{secrets.token_hex(4)}"
    admin = build_test_engine(db_preflight.url, db_preflight.schema)
    require_owned_sessionmaker(admin)
    async with admin.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{database}"'))
    url = make_url(db_preflight.url).set(database=database).render_as_string(hide_password=False)
    engine = build_test_engine(url, "public")
    require_owned_sessionmaker(engine)
    try:
        yield engine
    finally:
        await engine.dispose()
        try:
            async with admin.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        finally:
            await admin.dispose()


@pytest.mark.parametrize(
    "start",
    [
        "fresh",
        MAIN,
        "0111_routine_dm_destination",
        "0113_microsoft_accounts",
        "0110_cold_dm_agent",
        "0112_unattended_turn_provenance",
    ],
)
async def test_deployed_upgrade_and_downgrade_paths(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
) -> None:
    historical = _historical_scripts(tmp_path)
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            if start != "fresh":
                old_config = config if start == MAIN else _config(historical, sync)
                command.upgrade(old_config, start)
                assert sync.scalar(text("SELECT version_num FROM alembic_version")) == start
                # Sentinel rows prove the remap does not replay destructive DDL.
                sync.execute(
                    text(
                        "INSERT INTO tenants (platform, external_id) "
                        "VALUES ('discord', 'migration-test')"
                    )
                )
                if start != MAIN:
                    sync.execute(
                        text(
                            "INSERT INTO direct_message_policies "
                            "(tenant_id, enabled, dm_agent_name) "
                            "SELECT id, true, 'private' FROM tenants"
                        )
                    )
                if start in (
                    "0111_routine_dm_destination",
                    "0112_unattended_turn_provenance",
                    "0113_microsoft_accounts",
                ):
                    sync.execute(
                        text(
                            "INSERT INTO routines (tenant_id, created_by_user_id, agent_id, "
                            "agent_name, cron_expr, trigger_message, destination_kind, "
                            "destination_id, source_provenance_known, source_channel_id, source_thread_id) "
                            "SELECT id, 'owner', 'ag', 'private', '0 9 * * *', 'hello', 'dm', "
                            "'owner', true, 'source-channel', 'source-thread' FROM tenants"
                        )
                    )
                if start in (MAIN, "0113_microsoft_accounts"):
                    sync.execute(
                        text(
                            "INSERT INTO microsoft_user_tokens "
                            "(tenant_id, user_id, encrypted_refresh_token, scopes, "
                            "created_at, updated_at) "
                            "SELECT id, 'owner', decode('cafe', 'hex'), 'scope', "
                            "now(), now() FROM tenants"
                        )
                    )
                sync.commit()
            command.upgrade(config, PREVIOUS_HEAD)
            _assert_head(sync, PREVIOUS_HEAD)
            if start not in ("fresh", MAIN):
                assert (
                    sync.scalar(text("SELECT dm_agent_name FROM direct_message_policies"))
                    == "private"
                )
            if start in (MAIN, "0113_microsoft_accounts"):
                assert sync.scalar(
                    text("SELECT encrypted_refresh_token FROM microsoft_user_tokens")
                ) == bytes.fromhex("cafe")
            sync.commit()
            # A repeat upgrade is a no-op after translation.
            command.upgrade(config, PREVIOUS_HEAD)
            _assert_head(sync, PREVIOUS_HEAD)
            if start in (
                "0111_routine_dm_destination",
                "0112_unattended_turn_provenance",
                "0113_microsoft_accounts",
            ):
                assert sync.execute(
                    text(
                        "SELECT destination_kind, destination_id, source_provenance_known, "
                        "source_channel_id, source_thread_id FROM routines"
                    )
                ).one() == ("dm", "owner", True, "source-channel", "source-thread")
                sync.commit()
                with pytest.raises(RuntimeError, match="convert or remove DM routines"):
                    command.downgrade(config, MAIN_HEAD)
                # The failed downgrade rolls back the provenance downgrade too.
                _assert_head(sync, PREVIOUS_HEAD)
                sync.execute(text("DELETE FROM routines"))
                sync.commit()
            # Each new revision reverses, including Microsoft's tables.
            for target in reversed(
                [MAIN_HEAD, *CHAIN[CHAIN.index(MAIN_HEAD) + 1 : CHAIN.index(PREVIOUS_HEAD) + 1]]
            ):
                command.downgrade(config, target)
                assert sync.scalar(text("SELECT version_num FROM alembic_version")) == target
                inspector = inspect(sync)
                if target == "0135_routine_dm_destination":
                    assert "unattended_turn_event_id" not in {
                        c["name"] for c in inspector.get_columns("usage_reconcile_progress")
                    }
                if target == "0134_cold_dm_agent":
                    assert "source_provenance_known" not in {
                        c["name"] for c in inspector.get_columns("routines")
                    }
                if target == MAIN_HEAD:
                    assert "dm_agent_name" not in {
                        c["name"] for c in inspector.get_columns("direct_message_policies")
                    }
                    assert inspector.has_table("microsoft_user_tokens")
                if target == FORK:
                    assert not inspector.has_table("microsoft_user_tokens")
                    assert not inspector.has_table("microsoft_sign_ins")
                sync.commit()
            command.upgrade(config, LEAD_HEAD)
            _assert_head(sync, LEAD_HEAD)
            with pytest.raises(NotImplementedError, match="retention/export"):
                command.downgrade(config, PREVIOUS_HEAD)
            _assert_head(sync, LEAD_HEAD)
            command.upgrade(config, SANDBOX_HEAD)
            _assert_head(sync, SANDBOX_HEAD)
            assert (
                sync.scalar(text("SELECT enabled FROM watch_controls WHERE scope = 'global'"))
                is False
            )
            sync.commit()
            # The sandbox opt-in reverses; the watch evidence below it does not.
            command.downgrade(config, WATCH_POLLING)
            _assert_head(sync, WATCH_POLLING)
            with pytest.raises(NotImplementedError, match="Watch evidence"):
                command.downgrade(config, LEAD_HEAD)
            _assert_head(sync, WATCH_POLLING)
            command.upgrade(config, "head")
            _assert_head(sync)
            with pytest.raises(NotImplementedError, match="Recruitment evidence"):
                command.downgrade(config, SANDBOX_HEAD)
            _assert_head(sync)

        await connection.run_sync(exercise)


def _record_sql(statements: list[str]) -> Callable[..., None]:
    def record(
        _conn: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    return record


LEGACY = (
    *PREVIEW_IDS,
    "0110_cold_dm_agent",
    "0111_routine_dm_destination",
    "0112_unattended_turn_provenance",
    "0113_microsoft_accounts",
)


@pytest.mark.parametrize("start", ["empty", "unversioned", MAIN, *LEGACY])
@pytest.mark.parametrize("read_only", [False, True])
async def test_diagnostics_never_write_even_without_a_version_table(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
    read_only: bool,
) -> None:
    historical = (
        _preview_scripts(tmp_path) if start in PREVIEW_IDS else _historical_scripts(tmp_path)
    )
    canonical = tmp_path / "canonical"
    shutil.copytree(SCRIPTS, canonical, ignore=shutil.ignore_patterns("__pycache__"))
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(canonical, sync)
            if start == "unversioned":
                command.upgrade(config, FORK)
                sync.execute(text("DROP TABLE alembic_version"))
                sync.commit()
            elif start != "empty":
                command.upgrade(config if start == MAIN else _config(historical, sync), start)
            tables_before = set(inspect(sync).get_table_names())
            stamp_before = (
                sync.scalar(text("SELECT version_num FROM alembic_version"))
                if "alembic_version" in tables_before
                else None
            )
            sync.commit()
            if read_only:
                sync.execute(text("SET default_transaction_read_only = on"))
                sync.commit()
            statements: list[str] = []
            recorder = _record_sql(statements)
            event.listen(sync, "before_cursor_execute", recorder)
            try:
                # Graph-only commands must not enter env.py or contact the DB.
                command.history(config)
                command.heads(config)
                assert not statements
                if start in LEGACY:
                    diagnostics: list[Callable[[], object]] = [
                        lambda: command.current(config),
                        lambda: command.history(config, rev_range="current:head"),
                        lambda: command.history(config, indicate_current=True),
                        lambda: command.check(config),
                        lambda: command.stamp(config, MAIN),
                        lambda: command.stamp(config, MAIN, purge=True),
                        lambda: command.ensure_version(config),
                        lambda: command.revision(
                            config, message="must not write", autogenerate=True
                        ),
                        lambda: command.upgrade(config, f"{start}:head", sql=True),
                        lambda: command.downgrade(config, f"{start}:{MAIN}", sql=True),
                    ]
                    for action in diagnostics:
                        with pytest.raises(
                            util.CommandError, match=f"{start}.*alembic upgrade head"
                        ) as error:
                            action()
                        assert "Can't locate revision" not in str(error.value)
                else:
                    command.current(config)
                    with pytest.raises(util.CommandError):
                        command.check(config)
            finally:
                event.remove(sync, "before_cursor_execute", recorder)
            assert all(sql.lstrip().upper().startswith(("SELECT", "SHOW")) for sql in statements)
            assert set(inspect(sync).get_table_names()) == tables_before
            if stamp_before is not None:
                assert sync.scalar(text("SELECT version_num FROM alembic_version")) == stamp_before
            sync.commit()

        await connection.run_sync(exercise)


@pytest.mark.parametrize("revision", ["0092_routine_post_source", FORK, MAIN])
async def test_main_same_target_receives_no_repair_writes(
    migration_database: AsyncEngine,
    revision: str,
) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            command.upgrade(config, revision)
            statements: list[str] = []
            recorder = _record_sql(statements)
            event.listen(sync, "before_cursor_execute", recorder)
            try:
                command.upgrade(config, revision)
            finally:
                event.remove(sync, "before_cursor_execute", recorder)
            assert statements
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert not inspect(sync).has_table("microsoft_user_tokens") or revision == MAIN
            sync.commit()

        await connection.run_sync(exercise)


# Two independent processes intentionally bypass the deployer's single-migrator
# contract. PostgreSQL rejects one transaction; no custom lock/retry machinery.
_MIGRATOR = r"""
import asyncio
import sys
from alembic import command
from alembic.config import Config
from daimon.testing.db import build_test_engine, require_owned_sessionmaker
from sqlalchemy import text

async def main():
    engine = build_test_engine(sys.argv[1], "public")
    require_owned_sessionmaker(engine)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT set_config('application_name', :name, false)"), {"name": sys.argv[3]})
            await connection.commit()
            print("ready", flush=True)
            sys.stdin.readline()
            def migrate(sync):
                config = Config(sys.argv[2])
                config.attributes["connection"] = sync
                command.upgrade(config, "head")
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()
asyncio.run(main())
"""


async def _wait_for_backend(
    connection: AsyncConnection,
    name: str,
    wait_event: str,
    process: asyncio.subprocess.Process,
) -> None:
    async with asyncio.timeout(3):
        while True:
            if process.returncode is not None:
                output, _ = await process.communicate()
                raise AssertionError(output.decode())
            pid = await connection.scalar(
                text(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name = :name AND wait_event = :event"
                ),
                {"name": name, "event": wait_event},
            )
            await connection.commit()
            if pid is not None:
                return
            await asyncio.sleep(0.01)


async def test_concurrent_migrator_fails_cleanly_then_retry_succeeds(
    migration_database: AsyncEngine,
    tmp_path: Path,
) -> None:
    historical = _historical_scripts(tmp_path)
    async with migration_database.connect() as connection:

        def setup(sync: Connection) -> None:
            command.upgrade(_config(historical, sync), "0111_routine_dm_destination")
            sync.execute(
                text("INSERT INTO tenants (platform, external_id) VALUES ('discord', 'concurrent')")
            )
            sync.execute(
                text(
                    "INSERT INTO direct_message_policies (tenant_id, enabled, dm_agent_name) SELECT id, true, 'private' FROM tenants"
                )
            )
            sync.commit()

        await connection.run_sync(setup)
    processes: list[asyncio.subprocess.Process] = []
    url = migration_database.url.render_as_string(hide_password=False)
    try:
        # Preload both interpreters before taking a DDL barrier: the migration's
        # five-second lock_timeout must not include Python import/startup time.
        for label in ("migration-first", "migration-second"):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _MIGRATOR,
                url,
                str(ROOT / "alembic.ini"),
                label,
                cwd=ROOT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes.append(process)
            assert process.stdout is not None
            async with asyncio.timeout(60):
                while True:
                    line = await process.stdout.readline()
                    assert line, "migrator exited before ready"
                    if line == b"ready\n":
                        break
        async with (
            migration_database.connect() as barrier,
            migration_database.connect() as observer,
        ):
            observer = await observer.execution_options(isolation_level="AUTOCOMMIT")
            await barrier.execute(text("LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE"))
            first, second = processes
            assert first.stdin is not None and second.stdin is not None
            first.stdin.write(b"go\n")
            await first.stdin.drain()
            await _wait_for_backend(observer, "migration-first", "relation", first)
            second.stdin.write(b"go\n")
            await second.stdin.drain()
            await _wait_for_backend(observer, "migration-second", "transactionid", second)
            await barrier.rollback()
            outputs = await asyncio.wait_for(
                asyncio.gather(*(p.communicate() for p in processes)), 60
            )
            assert sorted(p.returncode for p in processes if p.returncode is not None) == [0, 1]
            loser = next(
                output
                for p, (output, _) in zip(processes, outputs, strict=True)
                if p.returncode == 1
            )
            assert any(
                message in loser
                for message in (b"duplicate key", b"already exists", b"lock timeout")
            ), loser.decode()
        async with migration_database.connect() as connection:

            def verify_and_retry(sync: Connection) -> None:
                _assert_head(sync)
                assert (
                    sync.scalar(text("SELECT dm_agent_name FROM direct_message_policies"))
                    == "private"
                )
                sync.commit()
                command.upgrade(_config(SCRIPTS, sync), "head")
                _assert_head(sync)

            await connection.run_sync(verify_and_retry)
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.communicate(), 10)


@pytest.mark.parametrize("start", ["empty", MAIN])
@pytest.mark.parametrize("operation", ["stamp", "ensure_version"])
async def test_bookkeeping_sql_matches_origin_main(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
    operation: str,
) -> None:
    # Frozen origin/main env; only the supplied-connection test entrypoint is
    # adapted. Use the same graph and connection to compare every SQL statement
    # AND bound parameter, without differences from driver/bootstrap setup.
    baseline = tmp_path / "main"
    shutil.copytree(SCRIPTS, baseline, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(Path(__file__).parent / "fixtures/preview_alembic_env.py", baseline / "env.py")
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            if start == MAIN:
                command.upgrade(_config(SCRIPTS, sync), MAIN)
            tables_before = set(inspect(sync).get_table_names())
            sync.commit()
            traces: list[list[tuple[str, str]]] = []
            for scripts in (baseline, SCRIPTS):
                if start == "empty":
                    sync.execute(text("DROP TABLE IF EXISTS alembic_version"))
                else:
                    sync.execute(
                        text("UPDATE alembic_version SET version_num = :revision"),
                        {"revision": MAIN},
                    )
                sync.commit()
                statements: list[tuple[str, str]] = []

                def record(
                    _conn: Connection,
                    _cursor: object,
                    sql: str,
                    parameters: object,
                    _context: ExecutionContext,
                    _executemany: bool,
                    _statements: list[tuple[str, str]] = statements,
                ) -> None:
                    _statements.append((sql, repr(parameters)))

                event.listen(sync, "before_cursor_execute", record)
                try:
                    config = _config(scripts, sync)
                    if operation == "stamp":
                        # Preserve the documented initial-schema squash recipe.
                        command.stamp(config, "0001_initial_schema")
                    else:
                        command.ensure_version(config)
                finally:
                    event.remove(sync, "before_cursor_execute", record)
                traces.append(statements)
                expected = (
                    "0001_initial_schema"
                    if operation == "stamp"
                    else (MAIN if start == MAIN else None)
                )
                assert sync.scalar(text("SELECT version_num FROM alembic_version")) == expected
                assert set(inspect(sync).get_table_names()) == tables_before | {"alembic_version"}
                sync.commit()
            assert traces[0]
            assert traces[1] == traces[0]

        await connection.run_sync(exercise)


@pytest.mark.parametrize("start", ["empty", MAIN, "0134_cold_dm_agent"])
async def test_purge_stamp_preserves_normal_nonlegacy_bookkeeping(
    migration_database: AsyncEngine,
    start: str,
) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            if start != "empty":
                command.upgrade(config, start)
            tables_before = set(inspect(sync).get_table_names())
            sync.commit()
            command.stamp(config, "0001_initial_schema", purge=True)
            assert (
                sync.scalar(text("SELECT version_num FROM alembic_version"))
                == "0001_initial_schema"
            )
            assert set(inspect(sync).get_table_names()) == tables_before | {"alembic_version"}
            sync.commit()
            command.ensure_version(config)
            assert (
                sync.scalar(text("SELECT version_num FROM alembic_version"))
                == "0001_initial_schema"
            )
            sync.commit()

        await connection.run_sync(exercise)


def _preview_scripts(tmp_path: Path) -> Path:
    """The byte-frozen scripts actually run by Preview at f4dcb43cc."""
    scripts = tmp_path / "deployed-preview"
    versions = scripts / "versions"
    versions.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fixtures/preview_alembic_env.py", scripts / "env.py")
    for source in (SCRIPTS / "versions").glob("*.py"):
        if source.stem not in CHAIN or source.stem == MAIN:
            shutil.copy(source, versions / source.name)
    for source in FROZEN_PREVIEW.glob("*.py"):
        shutil.copy(source, versions / source.name)
    return scripts


def _assert_usage_index_valid(sync: Connection) -> None:
    assert (
        sync.scalar(
            text(
                "SELECT indisvalid AND indisready FROM pg_index "
                "WHERE indexrelid = to_regclass('ix_usage_observation_tenant_id')"
            )
        )
        is True
    )
    assert inspect(sync).get_columns("alembic_version")[0]["type"].length >= max(map(len, CHAIN))


@pytest.mark.parametrize("start", [*PREVIEW_IDS, "0120_mcp_token_surface", MAIN_HEAD])
async def test_real_deployed_chains_upgrade_without_replaying_preview_data(
    migration_database: AsyncEngine, tmp_path: Path, start: str
) -> None:
    historical = _preview_scripts(tmp_path) if start in PREVIEW_IDS else SCRIPTS
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            command.upgrade(_config(historical, sync), start)
            sync.commit()
            inspector = inspect(sync)
            assert inspector.has_table("security_audit_events"), "0123 reads this historical table"
            sync.execute(
                text(
                    "INSERT INTO tenants (platform,external_id) VALUES ('discord','preserve-preview')"
                )
            )
            if inspector.has_table("lead_intake_configs"):
                sync.execute(
                    text(
                        "INSERT INTO lead_intake_configs (tenant_id,config,encrypted_credentials) "
                        "SELECT id,'{}'::jsonb,decode('cafe','hex') FROM tenants"
                    )
                )
            if inspector.has_table("recruitment_threads"):
                sync.execute(
                    text(
                        "INSERT INTO recruitment_threads (tenant_id,thread_id,parent_id,phase,"
                        "revision,encrypted_state,raw_expires_at,expires_at) "
                        "SELECT id,'thread','parent','preserved','revision',decode('beef','hex'),"
                        "now()+interval '1 day',now()+interval '7 days' FROM tenants"
                    )
                )
            if inspector.has_table("watch_controls"):
                sync.execute(
                    text("INSERT INTO watch_controls (scope,enabled) VALUES ('sentinel',true)")
                )
            if "dm_agent_name" in {
                col["name"] for col in inspector.get_columns("direct_message_policies")
            }:
                sync.execute(
                    text(
                        "INSERT INTO direct_message_policies (tenant_id,enabled,dm_agent_name) "
                        "SELECT id,true,'preserved-agent' FROM tenants"
                    )
                )
            # Freeze every old column of every old row. Main's new columns must not
            # conceal loss or rewriting of data already present on the Preview schema.
            queries = {
                table: "SELECT coalesce(jsonb_agg(to_jsonb(saved) ORDER BY to_jsonb(saved)::text),"
                "'[]'::jsonb) FROM (SELECT "
                + ",".join('"' + col["name"] + '"' for col in inspector.get_columns(table))
                + ' FROM "'
                + table
                + '") AS saved'
                for table in inspector.get_table_names()
                if table != "alembic_version"
            }
            before = {table: sync.scalar(text(query)) for table, query in queries.items()}
            sync.commit()
            config = _config(SCRIPTS, sync)
            statements: list[str] = []
            recorder = _record_sql(statements)
            event.listen(sync, "before_cursor_execute", recorder)
            try:
                command.upgrade(config, "head")
            finally:
                event.remove(sync, "before_cursor_execute", recorder)
            _assert_head(sync)
            _assert_usage_index_valid(sync)
            for table, query in queries.items():
                assert sync.scalar(text(query)) == before[table], table
            if start == "0122_recruitment":
                assert not any(
                    "UPDATE routines" in sql or "CREATE TABLE lead_" in sql for sql in statements
                ), "already-applied Preview cleanup and DDL must not replay"
            sync.commit()
            statements.clear()
            event.listen(sync, "before_cursor_execute", recorder)
            try:
                command.upgrade(config, "head")
            finally:
                event.remove(sync, "before_cursor_execute", recorder)
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            _assert_usage_index_valid(sync)
            sync.commit()

        await connection.run_sync(exercise)


@pytest.mark.parametrize("failure_point", ["index", "stamp"])
async def test_repair_failure_rolls_back_main_ddl_index_and_stamp_together(
    migration_database: AsyncEngine, tmp_path: Path, failure_point: str
) -> None:
    historical = _preview_scripts(tmp_path)
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            command.upgrade(_config(historical, sync), "0122_recruitment")
            sync.commit()
            tables_before = set(inspect(sync).get_table_names())
            sync.commit()

            def fail(
                _connection: Connection,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: ExecutionContext,
                _executemany: bool,
            ) -> None:
                if (
                    failure_point == "index"
                    and "CREATE INDEX IF NOT EXISTS ix_usage_observation" in statement
                ) or (failure_point == "stamp" and statement.startswith("UPDATE alembic_version")):
                    raise RuntimeError("injected repair failure")

            event.listen(sync, "after_cursor_execute", fail)
            try:
                with pytest.raises(RuntimeError, match="injected repair failure"):
                    command.upgrade(_config(SCRIPTS, sync), "head")
            finally:
                event.remove(sync, "after_cursor_execute", fail)
            sync.rollback()
            assert (
                sync.scalar(text("SELECT version_num FROM alembic_version")) == "0122_recruitment"
            )
            assert set(inspect(sync).get_table_names()) == tables_before
            assert "sender_foreign" not in {
                c["name"] for c in inspect(sync).get_columns("turn_origins")
            }
            assert "enabled_backends_explicit" not in {
                c["name"] for c in inspect(sync).get_columns("tenant_access_policies")
            }
            assert sync.scalar(text("SELECT to_regclass('ix_usage_observation_tenant_id')")) is None
            sync.commit()
            command.upgrade(_config(SCRIPTS, sync), "head")
            _assert_head(sync)
            _assert_usage_index_valid(sync)
            sync.commit()

        await connection.run_sync(exercise)


def test_main_migration_bodies_are_byte_identical_and_preview_fixture_is_frozen() -> None:
    import subprocess

    for name in subprocess.check_output(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            "4c151880377fff26887b5570cd8f1776a2880b49",
            "packages/core/alembic/versions",
        ],
        cwd=ROOT,
        text=True,
    ).splitlines():
        if not name.endswith(".py"):
            continue
        assert (ROOT / name).read_bytes() == subprocess.check_output(
            ["git", "show", f"4c151880377fff26887b5570cd8f1776a2880b49:{name}"], cwd=ROOT
        )
    for revision in PREVIEW_IDS:
        name = f"packages/core/alembic/versions/{revision}.py"
        assert (FROZEN_PREVIEW / f"{revision}.py").read_bytes() == subprocess.check_output(
            ["git", "show", f"f4dcb43cc2493ceef4123ec30f5837b20ca58dc9:{name}"], cwd=ROOT
        )


def _schema_contract(sync: Connection) -> dict[str, object]:
    """Compare constraints, indexes and function/security shape, independent of column order."""
    inspector = inspect(sync)
    tables: dict[str, object] = {}
    for table in inspector.get_table_names():
        tables[table] = {
            "columns": sorted(
                (column["name"], str(column["type"]), column["nullable"], str(column["default"]))
                for column in inspector.get_columns(table)
            ),
            "checks": sorted(
                (check["name"], check["sqltext"])
                for check in inspector.get_check_constraints(table)
            ),
            "primary_key": inspector.get_pk_constraint(table),
            "foreign_keys": sorted(
                inspector.get_foreign_keys(table), key=lambda key: key["name"] or ""
            ),
            "indexes": sorted(inspector.get_indexes(table), key=lambda index: index["name"]),
            "unique": sorted(
                inspector.get_unique_constraints(table), key=lambda key: key["name"] or ""
            ),
        }
    functions = sync.execute(
        text(
            "SELECT proname, pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid), "
            "prosecdef, proconfig, proacl::text FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' "
            "AND proname LIKE '%watch%' ORDER BY proname, pg_get_function_identity_arguments(p.oid)"
        )
    ).all()
    roles = sync.execute(
        text(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolbypassrls "
            "FROM pg_roles WHERE rolname IN ('daimon_sidecar','daimon_watch_ingress_owner') "
            "ORDER BY rolname"
        )
    ).all()
    rls = sync.execute(
        text(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
            "AND c.relkind='r' ORDER BY relname"
        )
    ).all()
    return {"tables": tables, "functions": functions, "roles": roles, "rls": rls}


async def test_fresh_and_repaired_preview_schemas_have_identical_constraints_and_security(
    migration_database: AsyncEngine, tmp_path: Path
) -> None:
    historical = _preview_scripts(tmp_path)
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            command.upgrade(config, "head")
            sync.commit()
            fresh = _schema_contract(sync)
            sync.execute(text("DROP SCHEMA public CASCADE"))
            sync.execute(text("CREATE SCHEMA public AUTHORIZATION pg_database_owner"))
            sync.commit()
            command.upgrade(_config(historical, sync), "0122_recruitment")
            sync.commit()
            command.upgrade(config, "head")
            sync.commit()
            assert _schema_contract(sync) == fresh
            _assert_usage_index_valid(sync)
            sync.commit()

        await connection.run_sync(exercise)


async def test_legacy_multihead_is_refused_before_any_repair_ddl(
    migration_database: AsyncEngine, tmp_path: Path
) -> None:
    historical = _preview_scripts(tmp_path)
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            command.upgrade(_config(historical, sync), "0122_recruitment")
            sync.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('0110_microsoft_accounts')")
            )
            sync.commit()
            before = _schema_contract(sync)
            sync.commit()
            with pytest.raises(util.CommandError, match="multi-head legacy"):
                command.upgrade(_config(SCRIPTS, sync), "head")
            sync.rollback()
            assert _schema_contract(sync) == before
            assert set(sync.scalars(text("SELECT version_num FROM alembic_version"))) == {
                "0122_recruitment",
                MAIN,
            }
            sync.commit()

        await connection.run_sync(exercise)

"""Exercise the canonical migration chain through real Alembic and PostgreSQL."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from daimon.core._models import Base
from daimon.testing.db import DatabasePreflight, build_test_engine, require_owned_sessionmaker
from sqlalchemy import Connection, Text, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

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


PREVIOUS_HEAD = "0140_clear_legacy_routine_output"


LEAD_HEAD = "0141_lead_intake"


WATCH_HEAD = "0142_watches"


WATCH_POLLING = "0143_watch_polling"


SANDBOX_HEAD = "0144_routine_sandbox_tools"


RECRUITMENT_HEAD = "0145_recruitment"


REMOVAL_HEAD = "0146_remove_recruitment"


LONG_JOBS_HEAD = "0147_long_jobs"


ADMINS_HEAD = "0148_daimon_admins"


ROUTINES_HEAD = "0151_routine_claimed_slot"


COMPUTE_SESSIONS_HEAD = "0152_compute_sessions"


REPO_MEMORY_HEAD = "0154_repo_memory_state"


COMPUTE_TASKS_HEAD = "0155_compute_tasks"


WATCH_MCP_HEAD = "0156_watch_mcp_sources"


NOTICE_RECEIPTS_HEAD = "0157_watch_notice_receipts"


ROUTINE_SOURCE_HEAD = "0161_routine_source_id"


CREDENTIALS_HEAD = "0162_watch_credentials"


DELIVERY_HEAD = "0163_watch_delivery"


LIFETIME_CAPS_HEAD = "0164_watch_lifetime_caps"


AGENT_TOOLS_HEAD = "0165_agent_daimon_tools"


HEAD = "0166_watch_notice_refused"


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
    RECRUITMENT_HEAD,
    REMOVAL_HEAD,
    LONG_JOBS_HEAD,
    ADMINS_HEAD,
    "0149_context_provider_bindings",
    "0150_origin_human_invocation",
    ROUTINES_HEAD,
    COMPUTE_SESSIONS_HEAD,
    "0153_repo_memory",
    REPO_MEMORY_HEAD,
    COMPUTE_TASKS_HEAD,
    WATCH_MCP_HEAD,
    NOTICE_RECEIPTS_HEAD,
    "0158_watch_mcp_claim",
    ROUTINE_SOURCE_HEAD,
    CREDENTIALS_HEAD,
    DELIVERY_HEAD,
    LIFETIME_CAPS_HEAD,
    AGENT_TOOLS_HEAD,
    HEAD,
]


SANDBOX_COLUMNS = {
    "sandbox_tools_enabled",
    "sandbox_tools_set_by",
    "sandbox_tools_set_at",
    "sandbox_agent_id",
    "sandbox_agent_version",
    "sandbox_environment_id",
    "sandbox_environment_updated_at",
}


TABLES = {
    "agent_daimon_tool_allowlists",
    "compute_sessions",
    "repo_memory",
    "repo_memory_sessions",
    "compute_tasks",
    "watch_notice_receipts",
    "watch_credentials",
    "daimon_admins",
    "context_provider_bindings",
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
    "long_job",
    "long_job_policy",
    "long_job_policy_history",
    "long_job_cancel_audit",
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


def _assert_head(connection: Connection, expected: str = HEAD) -> None:
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == expected

    compared_tables = (
        TABLES
        if expected
        in (
            LEAD_HEAD,
            WATCH_HEAD,
            WATCH_POLLING,
            SANDBOX_HEAD,
            RECRUITMENT_HEAD,
            REMOVAL_HEAD,
            LONG_JOBS_HEAD,
            ADMINS_HEAD,
            HEAD,
        )
        else {name for name in TABLES if not name.startswith("lead_")}
    )
    if expected not in (LONG_JOBS_HEAD, ADMINS_HEAD, HEAD):
        compared_tables = {name for name in compared_tables if not name.startswith("long_job")}
    if expected != HEAD:
        compared_tables = compared_tables - {
            "agent_daimon_tool_allowlists",
            "context_provider_bindings",
            "compute_sessions",
            "repo_memory",
            "repo_memory_sessions",
            "compute_tasks",
            "watch_notice_receipts",
            "watch_credentials",
        }
    if expected not in (ADMINS_HEAD, HEAD):
        compared_tables = compared_tables - {"daimon_admins"}
    if expected not in (
        SANDBOX_HEAD,
        RECRUITMENT_HEAD,
        REMOVAL_HEAD,
        LONG_JOBS_HEAD,
        ADMINS_HEAD,
        HEAD,
    ):
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
        if (
            kind == "column"
            and name
            in {
                "last_fired_slot",
                "last_finished_at",
                "last_finished_claim_at",
                "handback_paused_at",
                "handback_enabled",
                "handback_next_fire_at",
                "seeded_after",
                "source_routine_id",
                "internal_routine_id",
            }
            and expected != HEAD
        ):
            return False
        if (
            kind == "unique_constraint"
            and name in {"uq_routines_tenant_source_id", "uq_routines_tenant_internal_id"}
            and expected != HEAD
        ):
            return False
        # Historical main retains these two objects for older readers, while
        # its ORM intentionally omits them. These retained objects
        # are outside the ORM metadata comparison.
        if reflected and kind == "index" and name == "mcp_tokens_account_idx":
            return False
        if reflected and kind == "column" and name == "is_external":
            return getattr(getattr(obj, "table", None), "name", None) != "turn_origins"
        if (
            kind == "column"
            and name in {"human_invocation", "memory_read_only"}
            and getattr(getattr(obj, "table", None), "name", None) == "turn_origins"
            and expected != HEAD
        ):
            return False
        if kind == "column" and expected not in (
            SANDBOX_HEAD,
            RECRUITMENT_HEAD,
            REMOVAL_HEAD,
            LONG_JOBS_HEAD,
            ADMINS_HEAD,
            HEAD,
        ):
            return name not in SANDBOX_COLUMNS
        return kind != "table" or name in compared_tables

    migration_context = MigrationContext.configure(
        connection, opts={"include_name": include_name, "include_object": include_object}
    )
    assert not compare_metadata(migration_context, Base.metadata)
    inspector = inspect(connection)
    constraints = {c["name"] for c in inspector.get_check_constraints("routines")}
    assert {"ck_routines_destination_kind", "ck_routines_dm_owner"} <= constraints
    if expected in (
        SANDBOX_HEAD,
        RECRUITMENT_HEAD,
        REMOVAL_HEAD,
        LONG_JOBS_HEAD,
        ADMINS_HEAD,
        HEAD,
    ):
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


@pytest.mark.parametrize("start", ["fresh", MAIN])
async def test_deployed_upgrade_and_downgrade_paths(
    migration_database: AsyncEngine,
    start: str,
) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            if start != "fresh":
                command.upgrade(config, start)
                sync.execute(
                    text(
                        "INSERT INTO tenants (platform, external_id) "
                        "VALUES ('discord', 'migration-test')"
                    )
                )
                sync.execute(
                    text(
                        "INSERT INTO microsoft_user_tokens "
                        "(tenant_id, user_id, encrypted_refresh_token, scopes, created_at, updated_at) "
                        "SELECT id, 'owner', decode('cafe', 'hex'), 'scope', now(), now() FROM tenants"
                    )
                )
                sync.commit()
            command.upgrade(config, PREVIOUS_HEAD)
            _assert_head(sync, PREVIOUS_HEAD)
            if start == MAIN:
                assert sync.scalar(
                    text("SELECT encrypted_refresh_token FROM microsoft_user_tokens")
                ) == bytes.fromhex("cafe")
            sync.commit()
            # A repeat upgrade is a no-op.
            command.upgrade(config, PREVIOUS_HEAD)
            _assert_head(sync, PREVIOUS_HEAD)
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
            command.upgrade(config, RECRUITMENT_HEAD)
            _assert_head(sync, RECRUITMENT_HEAD)
            with pytest.raises(NotImplementedError, match="Recruitment evidence"):
                command.downgrade(config, SANDBOX_HEAD)
            _assert_head(sync, RECRUITMENT_HEAD)
            command.upgrade(config, LONG_JOBS_HEAD)
            _assert_head(sync, LONG_JOBS_HEAD)
            with pytest.raises(NotImplementedError, match="long-job execution evidence"):
                command.downgrade(config, RECRUITMENT_HEAD)
            _assert_head(sync, LONG_JOBS_HEAD)
            # Exercise reversible revisions before the retained Fly evidence.
            command.upgrade(config, ROUTINES_HEAD)
            assert sync.scalar(text("SELECT version_num FROM alembic_version")) == ROUTINES_HEAD
            principal = next(
                col
                for col in inspect(sync).get_columns("turn_origins")
                if col["name"] == "human_invocation"
            )
            assert principal["nullable"] is True
            assert principal["default"] is None
            command.downgrade(config, ADMINS_HEAD)
            _assert_head(sync, ADMINS_HEAD)
            assert not inspect(sync).has_table("context_provider_bindings")
            with pytest.raises(NotImplementedError, match="Delegation audit history"):
                command.downgrade(config, LONG_JOBS_HEAD)
            _assert_head(sync, ADMINS_HEAD)
            command.upgrade(config, COMPUTE_SESSIONS_HEAD)
            with pytest.raises(NotImplementedError, match="Retain compute ownership records"):
                command.downgrade(config, ROUTINES_HEAD)
            version = sync.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == COMPUTE_SESSIONS_HEAD
            command.upgrade(config, COMPUTE_TASKS_HEAD)
            with pytest.raises(NotImplementedError, match="Retain compute task records"):
                command.downgrade(config, REPO_MEMORY_HEAD)
            version = sync.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == COMPUTE_TASKS_HEAD
            command.upgrade(config, NOTICE_RECEIPTS_HEAD)
            command.downgrade(config, WATCH_MCP_HEAD)
            version = sync.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == WATCH_MCP_HEAD
            assert not inspect(sync).has_table("watch_notice_receipts")
            with pytest.raises(NotImplementedError, match="Watch evidence"):
                command.downgrade(config, COMPUTE_TASKS_HEAD)
            version = sync.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == WATCH_MCP_HEAD
            command.upgrade(config, CREDENTIALS_HEAD)
            version = sync.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == CREDENTIALS_HEAD
            # 0162 drops the owner watch keys and the form kind that collects them.
            command.downgrade(config, ROUTINE_SOURCE_HEAD)
            version = sync.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == ROUTINE_SOURCE_HEAD
            assert not inspect(sync).has_table("watch_credentials")
            command.downgrade(config, "0158_watch_mcp_claim")
            assert (
                sync.scalar(text("SELECT version_num FROM alembic_version"))
                == "0158_watch_mcp_claim"
            )
            # 0158 keeps watch history: no downgrade past it.
            with pytest.raises(NotImplementedError, match="Watch evidence"):
                command.downgrade(config, NOTICE_RECEIPTS_HEAD)
            command.upgrade(config, HEAD)
            _assert_head(sync)
            # 0163 and 0164 keep watch history: no downgrade past them.
            with pytest.raises(NotImplementedError, match="Watch evidence"):
                command.downgrade(config, DELIVERY_HEAD)
            _assert_head(sync)

        await connection.run_sync(exercise)


RECRUITMENT_TABLES = {
    "recruitment_threads",
    "recruitment_messages",
    "recruitment_outbox",
}


def _recruitment_catalog(connection: Connection) -> dict[str, object]:
    inspector = inspect(connection)
    return {
        table: {
            "columns": [
                (column["name"], str(column["type"]), column["nullable"], column["default"])
                for column in inspector.get_columns(table)
            ],
            "primary_key": inspector.get_pk_constraint(table),
            "foreign_keys": inspector.get_foreign_keys(table),
            "unique_constraints": inspector.get_unique_constraints(table),
            "indexes": inspector.get_indexes(table),
            "acl": connection.scalar(
                text("SELECT relacl::text FROM pg_class WHERE oid = to_regclass(:table)"),
                {"table": table},
            ),
        }
        for table in sorted(RECRUITMENT_TABLES)
    }


def _assert_recruitment_removed(connection: Connection) -> None:
    assert not RECRUITMENT_TABLES & set(inspect(connection).get_table_names())
    # No child indexes, constraints or ACL-bearing relations may survive.
    assert not connection.scalar(
        text(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND (relname LIKE 'recruitment_%' OR relname LIKE 'uq_recruitment_%')"
        )
    )
    assert not connection.scalar(
        text("SELECT count(*) FROM pg_constraint WHERE conname LIKE '%recruitment%'")
    )


@pytest.mark.parametrize("populated", [False, True], ids=["fresh", "populated-0145"])
async def test_remove_recruitment_roundtrip(
    migration_database: AsyncEngine, populated: bool
) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            before: dict[str, object] = {}
            if populated:
                command.upgrade(config, "0145_recruitment")
                before = _recruitment_catalog(sync)
                sync.execute(
                    text(
                        "INSERT INTO tenants (platform, external_id) VALUES ('discord', 'staffing')"
                    )
                )
                sync.execute(
                    text(
                        "INSERT INTO recruitment_threads "
                        "(tenant_id, thread_id, parent_id, phase, revision, encrypted_state, "
                        "raw_expires_at, expires_at) "
                        "SELECT id, 'thread', 'parent', 'open', 'v1', decode('cafe', 'hex'), "
                        "now(), now() FROM tenants"
                    )
                )
                sync.execute(
                    text(
                        "INSERT INTO recruitment_messages "
                        "(tenant_id, thread_id, message_id, raw_expires_at, observed_at, "
                        "source_channel_id, needs_enrichment, deleted, revision, encrypted_source) "
                        "SELECT id, 'thread', 'message', now(), now(), 'parent', false, false, "
                        "'v1', decode('cafe', 'hex') FROM tenants"
                    )
                )
                sync.execute(
                    text(
                        "INSERT INTO recruitment_outbox "
                        "(tenant_id, id, thread_id, revision, stable_key, state, encrypted_request) "
                        "SELECT id, 'intent', 'thread', 'v1', 'key', 'pending', "
                        "decode('cafe', 'hex') FROM tenants"
                    )
                )
                for table in RECRUITMENT_TABLES:
                    assert sync.scalar(text(f"SELECT count(*) FROM {table}")) == 1
                sync.commit()
            command.upgrade(config, REMOVAL_HEAD)
            _assert_head(sync, REMOVAL_HEAD)
            _assert_recruitment_removed(sync)
            command.downgrade(config, "0145_recruitment")
            assert (
                sync.scalar(text("SELECT version_num FROM alembic_version")) == "0145_recruitment"
            )
            if populated:
                assert _recruitment_catalog(sync) == before
                assert (
                    sync.scalar(text("SELECT count(*) FROM tenants WHERE external_id = 'staffing'"))
                    == 1
                )
            for table in RECRUITMENT_TABLES:
                assert sync.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                # PUBLIC and detector roles remain unable to read restored evidence.
                assert not sync.scalar(
                    text(
                        "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a "
                        "LEFT JOIN pg_roles r ON r.oid = a.grantee "
                        "WHERE c.oid = to_regclass(:table) "
                        "AND (a.grantee = 0 OR r.rolname IN "
                        "('daimon_sidecar', 'daimon_watch_ingress_owner'))"
                    ),
                    {"table": table},
                )
            sync.commit()
            command.upgrade(config, REMOVAL_HEAD)
            _assert_recruitment_removed(sync)

        await connection.run_sync(exercise)

"""Migration 0192 resets saved "Allow writes" approvals choices in the access policy."""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.feature_values import feature_revision
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0192_reset_allow_writes.py"
    spec = importlib.util.spec_from_file_location("migration_0192", path)
    assert spec and spec.loader, "the migration module must load"
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def _run(db: AsyncSession, step: str) -> None:
    migration = _load()
    conn = await db.connection()

    def run(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, step)()

    await conn.run_sync(run)


def _rule(approvals: str, **fields: str) -> ChannelRule:
    return ChannelRule(
        approvals=approvals,  # pyright: ignore[reportArgumentType]
        approvals_set_by=None if approvals == "inherit" else "admin-1",
        **fields,  # pyright: ignore[reportArgumentType]
    )


async def _stored(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, object]:
    row = (
        await db.execute(
            text("SELECT policy FROM tenant_access_policies WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalar_one()
    assert isinstance(row, dict), "the policy is stored as a JSON object"
    return row


async def test_upgrade_clears_allow_writes_on_workspace_channel_and_thread_only(
    db_session: AsyncSession,
) -> None:
    """Only "off" choices become inherit; "on" and inherit stay, and so do other tenants."""
    tenant, quiet = await make_tenant(db_session), await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            approvals="off",
            approvals_set_by="admin-1",
            channel_rules={
                "chan-off": _rule("off"),
                "chan-off-inside": _rule("off", readers="inside"),
                "thread-off": _rule("off"),
                "chan-on": _rule("on"),
                "chan-inherit": _rule("inherit", readers="inside"),
            },
        ),
    )
    await set_access_policy(
        db_session,
        tenant_id=quiet.id,
        policy=TenantAccessPolicy(
            approvals="on",
            approvals_set_by="admin-1",
            channel_rules={
                "chan-on": _rule("on"),
            },
        ),
    )
    before = await feature_revision(db_session, tenant_id=tenant.id)
    quiet_before = await feature_revision(db_session, tenant_id=quiet.id)
    quiet_stored = await _stored(db_session, quiet.id)

    await _run(db_session, "upgrade")

    stored = await _stored(db_session, tenant.id)
    assert "approvals" not in stored and "approvals_set_by" not in stored, (
        "the workspace Allow writes choice is cleared to inherit"
    )
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.approvals == "inherit", "the workspace choice reads as inherit"
    assert set(policy.channel_rules) == {"chan-off-inside", "chan-on", "chan-inherit"}, (
        "a rule left open by the reset is dropped, every other rule stays"
    )
    assert policy.channel_rules["chan-off-inside"] == ChannelRule(readers="inside"), (
        "a cleared rule keeps its other settings and loses its provenance"
    )
    assert policy.channel_rules["chan-on"].approvals == "on", "an Ask choice stays"
    assert policy.channel_rules["chan-inherit"].approvals == "inherit", "inherit stays"
    assert await feature_revision(db_session, tenant_id=tenant.id) > before, "caches refresh"
    assert await feature_revision(db_session, tenant_id=quiet.id) == quiet_before, (
        "a tenant with no Allow writes choice keeps its revision"
    )
    assert await _stored(db_session, quiet.id) == quiet_stored, "its policy is untouched"


async def test_upgrade_drops_the_rules_key_when_every_rule_was_only_allow_writes(
    db_session: AsyncSession,
) -> None:
    """The policy omits an empty rule list, so the migration leaves none behind."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(channel_rules={"chan-off": _rule("off")}),
    )
    await _run(db_session, "upgrade")
    assert "channel_rules" not in await _stored(db_session, tenant.id), (
        "no empty channel_rules is stored"
    )

"""`rename_agent_references` and `rename_selfhosted_agent` move live names, nothing else."""

from __future__ import annotations

import re
import uuid

import pytest
from daimon.core._models import Base, TenantAccessPolicyRecord
from daimon.core.stores.agent_avatars import get_or_create_avatar
from daimon.core.stores.agent_rename import (
    _KEYED_NAME_COLUMNS,  # pyright: ignore[reportPrivateUsage]
    _LIVE_NAME_COLUMNS,  # pyright: ignore[reportPrivateUsage]
    rename_agent_references,
    rename_policy_keys,
    rename_selfhosted_agent,
)
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.testing.factories import (
    make_account,
    make_routine,
    make_task_continuation,
    make_tenant,
)
from sqlalchemy import String, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_selfhosted_resources import agent_spec, scope


async def test_rename_references_when_names_differ_in_case_then_renames_only_that_tenant(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="guild-1")
    other = await make_tenant(db_session, workspace_id="guild-2")
    mine = await make_routine(db_session, tenant=tenant, agent_name="Daimon")
    copy = await make_routine(db_session, tenant=tenant, agent_name="daimon-copy")
    theirs = await make_routine(db_session, tenant=other, agent_name="daimon")

    await rename_agent_references(db_session, tenant_id=tenant.id, old="daimon", new="default")

    names = dict(
        (await db_session.execute(text("SELECT id, agent_name FROM routines"))).tuples().all()
    )
    assert names[mine.id] == "default", "a live reference matches case-insensitively"
    assert names[copy.id] == "daimon-copy", "a copy with a longer name keeps it"
    assert names[theirs.id] == "daimon", "another tenant's references are untouched"


@pytest.mark.fresh_schema
async def test_rename_references_when_historical_continuation_breaks_check_then_keeps_it_and_renames(
    db_session: AsyncSession,
) -> None:
    """A settled row with a retired reason is history: it keeps its name, live rows move."""
    tenant = await make_tenant(db_session, workspace_id="guild-1")
    account = await make_account(db_session, tenant=tenant)

    async def continuation() -> uuid.UUID:
        row = await make_task_continuation(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="c",
            thread_id=f"t-{uuid.uuid4()}",
            requester_account_id=account.id,
            requester_external_user_id="u",
            target_ma_agent_id="agent_1",
            target_name="daimon",
            reason="timer",
            idempotency_key=uuid.uuid4(),
        )
        return row.id

    history, live = await continuation(), await continuation()
    # Migrations 0169 and 0194 leave the check NOT VALID, so a retired reason can survive.
    await db_session.execute(
        text("ALTER TABLE task_continuations DROP CONSTRAINT ck_task_continuations_reason")
    )
    await db_session.execute(
        text(
            "UPDATE task_continuations SET reason = 'worker_finished', status = 'cancelled' "
            "WHERE id = :id"
        ),
        {"id": history},
    )
    await db_session.execute(
        text(
            "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
            "CHECK (reason IN ('task_handoff', 'private_input_applied', 'timer', "
            "'github_access_ready', 'watch_fired', 'job_finished', 'watch_report', "
            "'confirmed_call')) NOT VALID"
        )
    )

    await rename_agent_references(db_session, tenant_id=tenant.id, old="daimon", new="default")

    names = dict(
        (await db_session.execute(text("SELECT id, target_name FROM task_continuations")))
        .tuples()
        .all()
    )
    assert names[history] == "daimon", "a cancelled continuation is history and keeps its name"
    assert names[live] == "default", "a pending continuation follows the renamed agent"


async def test_rename_references_when_new_avatar_exists_then_keeps_both_rows(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="daimon")
    await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="default")

    await rename_agent_references(db_session, tenant_id=tenant.id, old="daimon", new="default")

    rows = (
        await db_session.execute(
            text("SELECT agent_name FROM agent_avatars WHERE tenant_id = :t ORDER BY 1"),
            {"t": tenant.id},
        )
    ).scalars()
    assert list(rows) == ["daimon", "default"], "the row already under the new name wins"


async def test_rename_references_when_skill_exists_under_new_name_in_other_case_then_keeps_both(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal = uuid.uuid4()
    for agent_name in ("daimon", "Default"):
        await db_session.execute(
            text(
                "INSERT INTO user_skills (tenant_id, principal_id, agent_name, name,"
                " source_repo_url, content_hash) VALUES (:t, :p, :a, 'notes', 'repo', 'h')"
            ),
            {"t": tenant.id, "p": principal, "a": agent_name},
        )

    await rename_agent_references(db_session, tenant_id=tenant.id, old="daimon", new="default")

    rows = (
        await db_session.execute(
            text("SELECT agent_name FROM user_skills WHERE tenant_id = :t ORDER BY 1"),
            {"t": tenant.id},
        )
    ).scalars()
    assert list(rows) == ["Default", "daimon"], "a case variant of the new name counts as taken"


async def test_rename_references_when_user_config_names_agent_then_moves_it(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session, workspace_id="guild-other")
    mine = await make_account(db_session, tenant=tenant)
    theirs = await make_account(db_session, tenant=other)
    for account in (mine, theirs):
        await db_session.execute(
            text("INSERT INTO user_config (account_id, agent_name) VALUES (:a, 'Daimon')"),
            {"a": account.id},
        )

    await rename_agent_references(db_session, tenant_id=tenant.id, old="daimon", new="default")

    names = dict(
        (await db_session.execute(text("SELECT account_id, agent_name FROM user_config")))
        .tuples()
        .all()
    )
    assert names[mine.id] == "default", "the tenant's own choice follows the rename"
    assert names[theirs.id] == "Daimon", "another tenant's choice is untouched"


@pytest.mark.parametrize(
    ("key", "rules", "expected"),
    [
        ("agent_rules", {"Daimon": {"runs_in": ["c1"]}}, {"default": {"runs_in": ["c1"]}}),
        (
            "agent_rules",
            {"daimon": {"runs_in": ["c1"]}, "default": {"runs_in": ["c2"]}},
            {"daimon": {"runs_in": ["c1"]}, "default": {"runs_in": ["c2"]}},
        ),
        (
            "agent_rules",
            {"daimon": {"runs_in": ["c1"]}, "Default": {"runs_in": ["c2"]}},
            {"daimon": {"runs_in": ["c1"]}, "Default": {"runs_in": ["c2"]}},
        ),
        ("agent_channel_pins", {"daimon": ["c1"]}, {"default": ["c1"]}),
    ],
    ids=["moved", "new-exists", "new-exists-other-case", "pins-moved"],
)
async def test_rename_references_when_policy_names_agent_then_moves_rule_unless_taken(
    key: str, rules: dict[str, object], expected: dict[str, object], db_session: AsyncSession
) -> None:
    tenant = await make_tenant(db_session)
    db_session.add(
        TenantAccessPolicyRecord(
            tenant_id=tenant.id,
            policy={key: rules, "channel_rules": {"c1": {}}},
        )
    )
    await db_session.flush()

    await rename_policy_keys(db_session, tenant_id=tenant.id, old="daimon", new="default")

    policy = await db_session.scalar(
        select(TenantAccessPolicyRecord.policy)
        .where(TenantAccessPolicyRecord.tenant_id == tenant.id)
        .execution_options(populate_existing=True)
    )
    assert policy is not None, "the policy row stays"
    assert policy[key] == expected, f"{key} after the rename: {policy}"
    assert policy["channel_rules"] == {"c1": {}}, "other policy keys are untouched"


@pytest.mark.parametrize(
    ("names", "result", "after"),
    [
        (["daimon"], "pending", ["default"]),
        (["daimon", "default"], "taken", ["daimon", "default"]),
        (["daimon-copy"], "absent", ["daimon-copy"]),
        (["daimon", "Daimon"], "ambiguous", ["Daimon", "daimon"]),
        (["member:daimon"], "absent", ["daimon"]),
    ],
    ids=["renamed", "taken", "absent", "case-duplicates", "member-owned"],
)
async def test_rename_selfhosted_when_agents_exist_then_reports_outcome(
    names: list[str],
    result: str,
    after: list[str],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    caller = scope(str(tenant.id), str(account.id))
    agents = SelfHostedAgents(db_session_factory)
    for entry in names:
        owner, _, name = entry.rpartition(":")
        metadata = {"daimon_name": name} | ({} if owner else {"daimon_managed": "true"})
        spec = agent_spec(name).model_copy(update={"metadata": metadata})
        await agents.create(caller, spec, key=f"create-{name}")

    async with db_session_factory() as session, session.begin():
        outcome = await rename_selfhosted_agent(
            session, tenant_id=tenant.id, old="daimon", new="default"
        )

    assert outcome == result, f"the rename reports {result!r} for {names}"
    rows = (
        await db_session.execute(
            text(
                "SELECT name, spec->>'name', spec->'metadata'->>'daimon_name' FROM selfhosted_agents"
                " WHERE tenant_id = :t AND archived_at IS NULL ORDER BY 1"
            ),
            {"t": tenant.id},
        )
    ).tuples()
    assert [(n, n, n) for n in after] == list(rows), "record, spec and metadata agree on the name"
    revisions = (
        await db_session.execute(
            text("SELECT DISTINCT revision FROM selfhosted_agents WHERE tenant_id = :t"),
            {"t": tenant.id},
        )
    ).scalars()
    assert list(revisions) == [1], "the rename keeps the revision a running turn pinned"


# Columns that name an agent but keep the name they hold, with why.
_KEPT_NAME_COLUMNS = {
    ("agent_tool_rule_history", "before_agent_name"): "history keeps the name it recorded",
    ("agent_tool_rule_history", "after_agent_name"): "history keeps the name it recorded",
    ("security_audit_events", "agent_name"): "audit keeps the name it recorded",
    ("web_audit", "target"): "audit keeps the name it recorded",
    ("neutral_skill_versions", "agent_name"): "the neutral catalog is immutable",
    ("credential_requests", "target"): "a credential's target, not an agent",
    ("session_preparations", "target_fingerprint"): "a hash, not a name",
    ("selfhosted_agent_versions", "name"): "a revision keeps the name it was saved under",
    ("turn_origins", "helper_key"): "a helper turn's session key, not a name",
}
# Moved outside the column lists: by `rename_selfhosted_agent` and by hand.
_OTHER_RENAMED = {("selfhosted_agents", "name"), ("user_config", "agent_name")}
_AGENT_LIKE = re.compile(r"agent|responder|helper|target")


def test_agent_name_columns_when_schema_walked_then_each_is_renamed_or_kept() -> None:
    found = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, String)
        and not column.name.endswith("_id")
        and (_AGENT_LIKE.search(column.name) or (column.name == "name" and "agent" in table.name))
    }
    handled = {
        *_LIVE_NAME_COLUMNS,
        *((table, column) for table, column, _ in _KEYED_NAME_COLUMNS),
        *_OTHER_RENAMED,
        *_KEPT_NAME_COLUMNS,
    }
    assert found <= handled, f"rename or keep each agent-name column: {found - handled}"
    assert handled <= found, f"drop entries for columns that are gone: {handled - found}"


# JSON payloads that could hold an agent name, with why none is moved.
_KEPT_JSON_COLUMNS = {
    ("agent_drafts", "config"): "keyed by agent id; a name edit is refused and never written",
    ("agent_drafts", "base_config"): "the same draft's starting point",
    ("channel_config_revision", "revision"): "backend, profile and model only, and immutable",
    ("thread_lease", "active"): "a holder and a fence, no agent name",
    ("wizard_session", "spec"): "a form's prompt and steps, no agent name",
}


def test_kept_json_columns_when_schema_walked_then_still_exist() -> None:
    found = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
    }
    assert set(_KEPT_JSON_COLUMNS) <= found, (
        f"drop entries for columns that are gone: {set(_KEPT_JSON_COLUMNS) - found}"
    )

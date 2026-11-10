"""`rename_agent_references` and `rename_selfhosted_agent` move live names, nothing else."""

from __future__ import annotations

import uuid

import pytest
from daimon.core._models import TenantAccessPolicyRecord
from daimon.core.stores.agent_avatars import get_or_create_avatar
from daimon.core.stores.agent_rename import rename_agent_references, rename_selfhosted_agent
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.testing.factories import make_account, make_routine, make_tenant
from sqlalchemy import select, text
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

    await rename_agent_references(db_session, tenant_id=tenant.id, old="daimon", new="default")

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
        (["daimon"], "renamed", ["default"]),
        (["daimon", "default"], "taken", ["daimon", "default"]),
        (["daimon-copy"], "absent", ["daimon-copy"]),
        (["daimon", "Daimon"], "ambiguous", ["Daimon", "daimon"]),
    ],
    ids=["renamed", "taken", "absent", "case-duplicates"],
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
    for name in names:
        await agents.create(caller, agent_spec(name), key=f"create-{name}")

    async with db_session_factory() as session, session.begin():
        outcome = await rename_selfhosted_agent(
            session, tenant_id=tenant.id, old="daimon", new="default"
        )

    assert outcome == result, f"the rename reports {result!r} for {names}"
    rows = (
        await db_session.execute(
            text(
                "SELECT name, spec->>'name' FROM selfhosted_agents"
                " WHERE tenant_id = :t AND archived_at IS NULL ORDER BY 1"
            ),
            {"t": tenant.id},
        )
    ).tuples()
    assert [(n, n) for n in after] == list(rows), "the record and its spec carry the same name"
    revisions = (
        await db_session.execute(
            text("SELECT DISTINCT revision FROM selfhosted_agents WHERE tenant_id = :t"),
            {"t": tenant.id},
        )
    ).scalars()
    assert list(revisions) == [1], "the rename keeps the revision a running turn pinned"

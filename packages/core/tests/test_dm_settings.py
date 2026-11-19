"""Admin DM settings: a rule named by its position is read and written under one lock."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core import dm_settings
from daimon.core.authz import AgentRef
from daimon.core.dm_access import DmAgent
from daimon.core.dm_settings import DmActor, DmRuleSpec
from daimon.core.errors import UserFacingError
from daimon.core.github_credentials import encrypt_token
from daimon.core.native_admins import GroupLookupFailed
from daimon.core.stores import dm_access, web_audit
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 1, 1, tzinfo=UTC)
AGENTS = [DmAgent(name, f"ag_{name}", AgentRef.of(name)) for name in ("a", "b", "c")]


async def test_change_rule_when_a_removal_commits_first_then_changes_the_rule_now_there(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> None:
    """The change waits for the removal's lock, then reads the list it left, so its write
    and its audit row name the same rule."""
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = DmActor(tenant.id, None, NOW)
    rules = [DmRuleSpec("group", "g1", "a"), DmRuleSpec("group", "g2", "b")]
    async with factory.begin() as session:
        await dm_settings.replace_rules(session, actor, AGENTS, rules)
    removed, release = asyncio.Event(), asyncio.Event()

    async def remove() -> None:
        async with factory.begin() as session:
            assert await dm_settings.remove_rule(session, actor, 0), "the first rule goes"
            removed.set()
            await release.wait()

    async def change() -> bool:
        async with factory.begin() as session:
            return await dm_settings.change_rule(
                session, actor, AGENTS, 0, agent="c", locked=False, to=None
            )

    first = asyncio.create_task(remove())
    await removed.wait()
    second = asyncio.create_task(change())
    await asyncio.sleep(0.2)
    assert not second.done(), "the change waits for the removal to commit"
    release.set()
    await first
    assert await second, "a rule is still at the position"
    async with factory() as session:
        stored = await dm_access.list_rules(session, tenant_id=tenant.id)
        rows = await web_audit.list_recent(session, tenant_id=tenant.id, action="Changed DM rule")
    assert [(r.subject_id, r.agent_name) for r in stored] == [("g2", "c")], stored
    assert [r.detail for r in rows] == ["Group g2 to c"], "the audit row names the rule changed"


async def test_new_groups_when_slack_lacks_the_scope_then_refused_with_a_reinstall_message(
    db_session: AsyncSession,
) -> None:
    """A group Slack cannot read is refused in a grant and a rule; other failures and users pass."""
    tenant = await make_tenant(db_session)
    actor = DmActor(tenant.id, None, NOW)

    def failing(error: str) -> dm_settings.GroupsProbe:
        async def probe() -> object:
            raise GroupLookupFailed(error)

        return probe

    scope = failing("missing_scope")
    with pytest.raises(UserFacingError, match="Reinstall"):
        await dm_settings.set_grant(
            db_session, actor, AGENTS[0], "a", who="listed", user_ids=[], group_ids=["g1"],
            probe=scope,
        )  # fmt: skip
    with pytest.raises(UserFacingError, match="Reinstall"):
        await dm_settings.add_rule(
            db_session, actor, AGENTS, DmRuleSpec("group", "g1", "a"), None, probe=scope
        )
    assert await dm_settings.set_grant(
        db_session, actor, AGENTS[0], "a", who="listed", user_ids=["u1"], group_ids=[],
        probe=scope,
    ), "people need no scope"  # fmt: skip
    assert await dm_settings.set_grant(
        db_session, actor, AGENTS[0], "a", who="listed", user_ids=["u1"], group_ids=["g1"],
        probe=failing("ClientError"),
    ), "another failure is not a missing scope"  # fmt: skip


async def test_slack_groups_probe_asks_slack_when_made_and_replays_the_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Slack is asked once, before any change opens its transaction; the probe only replays."""
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id="T1")
        await upsert_slack_bot_token(
            session, team_id="T1", encrypted_token=encrypt_token(fernet, "xoxb-1")
        )
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": False, "error": "missing_scope"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
        probe = await dm_settings.slack_groups_probe(
            db_session_factory, tenant.id, fernet=fernet, http=http
        )
    assert probe is not None and len(calls) == 1, "Slack was asked before the probe was used"
    with pytest.raises(GroupLookupFailed, match="missing_scope"):
        await probe()
    assert len(calls) == 1, "using the probe asks nothing"

"""A workspace that set up no DM routing gets the DM result it had before routing.

Unrouted means no rule and no grant other than every member (`dm_routing.routed`). There
only the default agent starts a DM, and picks are ignored. With no default, each platform
keeps its earlier rule, a deliberate difference: Discord and Slack refuse a cold DM with
the `/dm-me` guidance, and a Teams personal chat answers with the chat's own agent.
`/dm-me` and hand-overs still reach any agent the person may use. The core paths are
checked here; each adapter's handling of the result is pinned by the tests in `ADAPTERS`.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from daimon.adapters.cli.commands.dm import explain_route
from daimon.adapters.web.admin_dm import explain_json
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import AgentRef, build_subject
from daimon.core.config import DatabaseSettings, Settings
from daimon.core.dm_access import DmAgent, decide_dm, permits_dm
from daimon.core.dm_explain import explain_dm
from daimon.core.dm_routing import DmRefused, DmStart
from daimon.core.stores import dm_access as store
from daimon.core.stores.direct_messages import set_dm_agent_name, set_dm_enabled
from daimon.core.stores.domain import Platform, TenantRow
from daimon.testing.factories import make_account, make_tenant
from pydantic import PostgresDsn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_dm_group_lookups import SOURCES

NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = TenantAccessPolicy()
USER = "11"
SUBJECT = build_subject(is_admin=False, platform_user_id=USER, verified_membership=True)
AGENTS = [DmAgent(n, f"ag_{n}", AgentRef.of(n)) for n in ("a", "b")]
ROOT = Path(__file__).resolve().parents[2]
SETTINGS = Settings.model_validate(
    {"database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d"))}
)

# Each module that turns a DM decision into what the person sees, and the tests pinning
# what an unrouted workspace gets there (the same as before routing).
ADAPTERS: dict[str, tuple[str, ...]] = {
    "adapters/discord/daimon/adapters/discord/commands/direct_messages.py": (
        "packages/adapters/discord/tests/commands/test_cold_direct_messages.py::"
        "test_cold_dm_when_unrouted_then_only_the_default_answers",
    ),
    "adapters/slack/daimon/adapters/slack/direct_messages.py": (
        "packages/adapters/slack/tests/test_cold_dm.py::"
        "test_cold_dm_when_unrouted_and_no_default_then_the_guidance",
    ),
    "adapters/teams/daimon/adapters/teams/personal_chat.py": (
        "packages/adapters/teams/tests/test_personal_chat.py::"
        "test_route_personal_chat_when_unrouted_then_the_chats_own_agent_answers",
    ),
    "core/daimon/core/dm_explain.py": (
        "tests/parity/test_dm_unrouted.py::test_explain_when_unrouted_then_says_what_answers",
    ),
}


async def _workspace(
    factory: async_sessionmaker[AsyncSession], platform: Platform
) -> tuple[TenantRow, uuid.UUID]:
    """DMs on and nothing else set; a pick for `a` is saved for the person."""
    async with factory.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id="700000303")
        account = await make_account(session, tenant=tenant)
        if platform != "teams":  # Teams personal chats start on
            await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await store.set_pick(
            session, tenant_id=tenant.id, account_id=account.id, agent_name="a", now=NOW
        )
    return tenant, account.id


async def _decide(factory: async_sessionmaker[AsyncSession], tenant: TenantRow, account: uuid.UUID):
    async with factory() as session:
        return await decide_dm(
            session, None, POLICY, tenant_id=tenant.id, platform=tenant.platform,
            agents=AGENTS, subject=SUBJECT, account_id=account,
        )  # fmt: skip


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_decide_when_unrouted_then_nothing_new_starts_a_dm(
    db_session_factory: async_sessionmaker[AsyncSession], platform: Platform
) -> None:
    """No sole-agent start, no picker, no pick: only a default, as before routing."""
    tenant, account = await _workspace(db_session_factory, platform)
    decision = await _decide(db_session_factory, tenant, account)
    assert decision.route == DmRefused("unrouted"), "the platform's own rule applies"
    async with db_session_factory() as session:
        for agent in AGENTS:  # `/dm-me` and hand-overs, as before routing
            assert await permits_dm(
                session, None, POLICY, tenant_id=tenant.id, platform=platform,
                agent=agent, subject=SUBJECT,
            ), agent.name  # fmt: skip
    async with db_session_factory.begin() as session:
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="b")
    decision = await _decide(db_session_factory, tenant, account)
    assert decision.route == DmStart("b", "default"), "the default answers, not the pick"


def _member(request: httpx.Request) -> httpx.Response:
    """Discord's member read for `USER`, holding no role; other platforms read nothing."""
    return httpx.Response(200, json={"user": {"id": USER}, "roles": []})


@pytest.mark.parametrize(
    ("platform", "cli", "web"),
    [
        ("discord", "refused (unrouted)", ("refused", None)),
        ("slack", "refused (unrouted)", ("refused", None)),
        ("teams", "starts with the chat's own agent (unrouted)", ("start", None)),
    ],
)
async def test_explain_when_unrouted_then_says_what_answers(
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: Platform,
    cli: str,
    web: tuple[str, str | None],
) -> None:
    """`dm explain` and "Check a person" say what the platform does, not a picker."""
    tenant, _ = await _workspace(db_session_factory, platform)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_member)) as http:
        found = await explain_dm(
            db_session_factory, SETTINGS, http, tenant=tenant, agents=AGENTS, user_id=USER
        )
    shown = explain_json(found)
    assert explain_route(found) == cli, "the CLI line"
    assert (shown["route"], shown["agent"]) == web, shown
    assert shown["reason"] == "unrouted", shown


def _tests(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text())
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


def test_every_module_that_acts_on_a_dm_decision_pins_the_unrouted_result() -> None:
    """A new module reading `decide_dm` or `explain_dm` says what an unrouted workspace gets."""
    deciders = {
        path
        for path, tree in SOURCES.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "decide_dm"
    }
    core = {"core/daimon/core/dm_access.py"}  # `permits_dm` itself, checked above
    assert deciders - core == set(ADAPTERS), "add the module and its unrouted test to ADAPTERS"
    for tests in ADAPTERS.values():
        for test in tests:
            path, name = test.split("::")
            assert name in _tests(path), test

"""`daimon dm default | access | rule | explain | import-policies` over the stored access."""

from __future__ import annotations

import json
from io import StringIO

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands.dm import (
    dm_access,
    dm_default,
    dm_explain,
    dm_import_policies,
    dm_rules,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.adapters.cli.tenant import TenantSelector
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.errors import StoreError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.direct_messages import dm_enabled, set_dm_enabled
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_agents import _agent_json  # pyright: ignore[reportPrivateUsage]


def _console() -> tuple[Console, StringIO]:
    out = StringIO()
    return Console(file=out, width=200, highlight=False), out


def _runtime(
    sessions: async_sessionmaker[AsyncSession],
    agents: list[dict[str, object]],
    settings: Settings | None = None,
) -> CliRuntime:
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response(agents))
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(router.dispatch), base_url="https://api.anthropic.com"
    )
    return CliRuntime(
        settings=settings
        or Settings(_env_file=None, database={"url": "postgresql+asyncpg://u:p@h/db"}),  # pyright: ignore[reportCallIssue, reportArgumentType]
        anthropic=AsyncAnthropic(api_key="test", http_client=http),
        sessionmaker=sessions,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


async def _tenant(sessions: async_sessionmaker[AsyncSession]):
    async with sessions.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="g1")
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    return tenant, TenantSelector(tenant_id=str(tenant.id), guild_id=None)


async def test_default_sets_shows_and_clears_the_default_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The default agent is stored per workspace and cleared with --clear."""
    _, selector = await _tenant(db_session_factory)
    rt = _runtime(db_session_factory, [])
    console, out = _console()
    await dm_default(
        rt=rt, console=console, selector=selector, agent="ada", clear=False, as_json=True
    )
    assert json.loads(out.getvalue())["default_agent"] == "ada", "the agent is set"
    out.truncate(0)
    out.seek(0)
    await dm_default(
        rt=rt, console=console, selector=selector, agent=None, clear=True, as_json=True
    )
    assert json.loads(out.getvalue())["default_agent"] is None, "--clear removes it"


async def test_access_and_rules_round_trip_and_explain_names_the_route(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A list on one agent and a locked rule on another decide where a person lands."""
    tenant, selector = await _tenant(db_session_factory)
    agents = [
        _agent_json(agent_id="ag_a", name="ada", tenant_id=tenant.id),
        _agent_json(agent_id="ag_b", name="bob", tenant_id=tenant.id),
    ]
    rt = _runtime(db_session_factory, agents)
    console, out = _console()
    await dm_access(
        rt=rt, console=console, selector=selector, agent="ada", mode="listed",
        users=["7"], groups=[], as_json=True,
    )  # fmt: skip
    assert json.loads(out.getvalue())["who"] == "listed", "the grant is stored"
    out.truncate(0)
    out.seek(0)
    await dm_rules(
        rt=rt, console=console, selector=selector, action="add", user="8", agent="bob",
        locked=True, as_json=True,
    )  # fmt: skip
    assert json.loads(out.getvalue())[0]["locked"] is True, "the rule is stored"
    out.truncate(0)
    out.seek(0)
    await dm_explain(rt=rt, console=console, selector=selector, user="8", groups=[], as_json=True)
    assert "starts with bob (locked)" in json.loads(out.getvalue())["route"], "the lock decides"
    out.truncate(0)
    out.seek(0)
    await dm_explain(rt=rt, console=console, selector=selector, user="7", groups=[], as_json=True)
    assert json.loads(out.getvalue())["allowed"] == ["ada", "bob"], "the list admits them"


async def test_rule_remove_for_a_missing_position_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A position with no rule is an error, not a silent no-op."""
    _, selector = await _tenant(db_session_factory)
    console, _ = _console()
    with pytest.raises(StoreError):
        await dm_rules(
            rt=_runtime(db_session_factory, []), console=console, selector=selector,
            action="remove", position=3,
        )  # fmt: skip


async def test_import_policies_lists_allowlisted_users_and_is_idempotent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An env allowlist becomes a listed grant on each agent; a second run does nothing."""
    tenant, selector = await _tenant(db_session_factory)
    settings = Settings(
        _env_file=None,
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        direct_message_policies={
            tenant.id: DirectMessagePolicy(mode="allowlist", recipient_ids=["7", "9"])
        },
    )
    rt = _runtime(
        db_session_factory,
        [_agent_json(agent_id="ag_a", name="ada", tenant_id=tenant.id)],
        settings,
    )
    console, out = _console()
    await dm_import_policies(rt=rt, console=console, dry_run=True, as_json=True)
    assert len(json.loads(out.getvalue())) == 1, "a dry run reports the step"
    out.truncate(0)
    out.seek(0)
    await dm_import_policies(rt=rt, console=console, dry_run=False, as_json=True)
    out.truncate(0)
    out.seek(0)
    await dm_access(
        rt=rt, console=console, selector=selector, agent="ada", mode=None, users=[], groups=[],
        as_json=True,
    )  # fmt: skip
    assert json.loads(out.getvalue())["users"] == ["7", "9"], "the allowlist is stored"
    out.truncate(0)
    out.seek(0)
    await dm_import_policies(rt=rt, console=console, dry_run=False, as_json=True)
    assert json.loads(out.getvalue()) == [], "a second run changes nothing"


async def test_import_policies_turns_a_disabled_workspace_off(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `disabled` env policy turns the workspace's private messages off."""
    tenant, _ = await _tenant(db_session_factory)
    settings = Settings(
        _env_file=None,
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        direct_message_policies={tenant.id: DirectMessagePolicy(mode="disabled")},
    )
    console, _ = _console()
    await dm_import_policies(
        rt=_runtime(db_session_factory, [], settings), console=console, dry_run=False
    )
    async with db_session_factory() as session:
        assert not await dm_enabled(session, tenant_id=tenant.id), "private messages are off"

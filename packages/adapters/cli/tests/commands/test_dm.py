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
from daimon.core.errors import StoreError, UserFacingError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import dm_access as dm_store
from daimon.core.stores import web_audit
from daimon.core.stores.direct_messages import (
    dm_enabled,
    dm_turned_off,
    get_dm_agent_name,
    set_dm_agent_name,
    set_dm_enabled,
)
from daimon.core.stores.identity import get_or_create_cli_principal
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
    tenant, selector = await _tenant(db_session_factory)
    rt = _runtime(
        db_session_factory, [_agent_json(agent_id="ag_a", name="ada", tenant_id=tenant.id)]
    )
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
    await dm_explain(rt=rt, console=console, selector=selector, user="8", as_json=True)
    assert "starts with bob (locked)" in json.loads(out.getvalue())["route"], "the lock decides"
    out.truncate(0)
    out.seek(0)
    await dm_explain(rt=rt, console=console, selector=selector, user="7", as_json=True)
    found = json.loads(out.getvalue())
    assert found["allowed"] == ["ada", "bob"], "the list admits them"
    assert found["groups"] == [], "no rule names a group, so none is looked up"


async def test_explain_when_the_env_policy_leaves_them_out_then_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Explain applies the deprecated env allowlist, as delivery does."""
    tenant, selector = await _tenant(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="ada")
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        direct_message_policies={
            tenant.id: DirectMessagePolicy(mode="allowlist", recipient_ids=["7"])
        },
    )
    rt = _runtime(
        db_session_factory,
        [_agent_json(agent_id="ag_a", name="ada", tenant_id=tenant.id)],
        settings,
    )
    console, out = _console()
    for user, route in (("7", "starts with ada (default)"), ("8", "refused (no_agent)")):
        await dm_explain(rt=rt, console=console, selector=selector, user=user, as_json=True)
        assert json.loads(out.getvalue())["route"] == route, user
        out.truncate(0)
        out.seek(0)


async def test_writes_when_the_agent_is_unknown_then_refused_and_nothing_changes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """As from the web app: a mistyped agent name is refused, not stored."""
    tenant, selector = await _tenant(db_session_factory)
    rt = _runtime(
        db_session_factory, [_agent_json(agent_id="ag_a", name="ada", tenant_id=tenant.id)]
    )
    console, _ = _console()
    with pytest.raises(UserFacingError, match="no agent called adaa"):
        await dm_default(rt=rt, console=console, selector=selector, agent="adaa", clear=False)
    with pytest.raises(UserFacingError, match="no agent called adaa"):
        await dm_access(
            rt=rt, console=console, selector=selector, agent="adaa", mode="listed",
            users=["7"], groups=[],
        )  # fmt: skip
    with pytest.raises(UserFacingError, match="no agent called adaa"):
        await dm_rules(
            rt=rt, console=console, selector=selector, action="add", everyone=True, agent="adaa"
        )
    async with db_session_factory() as session:
        assert await get_dm_agent_name(session, tenant_id=tenant.id) is None
        assert await dm_store.list_grants(session, tenant_id=tenant.id) == {}
        assert await dm_store.list_rules(session, tenant_id=tenant.id) == []
        assert await web_audit.list_recent(session, tenant_id=tenant.id) == []


async def test_writes_leave_one_audit_row_each(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same rows as a change from the web app, by the CLI's local user."""
    tenant, selector = await _tenant(db_session_factory)
    rt = _runtime(
        db_session_factory, [_agent_json(agent_id="ag_a", name="ada", tenant_id=tenant.id)]
    )
    console, _ = _console()
    await dm_default(rt=rt, console=console, selector=selector, agent="ada", clear=False)
    await dm_access(
        rt=rt, console=console, selector=selector, agent="ada", mode="listed",
        users=["7"], groups=[],
    )  # fmt: skip
    for group in ("g1", "g2"):
        await dm_rules(
            rt=rt, console=console, selector=selector, action="add", group=group, agent="ada"
        )
    await dm_rules(rt=rt, console=console, selector=selector, action="move", position=1, to=0)
    await dm_rules(rt=rt, console=console, selector=selector, action="remove", position=1)
    async with db_session_factory.begin() as session:
        rows = await web_audit.list_recent(session, tenant_id=tenant.id)
        cli = await get_or_create_cli_principal(
            session, tenant_id=tenant.id, os_user=rt.settings.cli.local_user
        )
    assert sorted((r.action, r.detail) for r in rows) == [
        ("Added DM rule", "Group g1 to ada"),
        ("Added DM rule", "Group g2 to ada"),
        ("Changed DM access", "Listed: 1 person"),
        ("Changed DM default agent", "ada"),
        ("Moved DM rule", "Group g2: 2 to 1"),
        ("Removed DM rule", "Group g1 to ada"),
    ]
    assert {r.actor_account_id for r in rows} == {cli.account_id}, "the CLI's local user"


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
    """An allowlist needs a default agent, becomes a listed grant on each agent, then is stable."""
    tenant, selector = await _tenant(db_session_factory)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
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
    with pytest.raises(UserFacingError, match="daimon dm default"):
        await dm_import_policies(rt=rt, console=console, dry_run=False, as_json=True)
    async with db_session_factory.begin() as session:
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="ada")
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
    async with db_session_factory() as session:
        rows = await web_audit.list_recent(session, tenant_id=tenant.id)
    assert [(r.action, r.actor_account_id is not None) for r in rows] == [
        ("Changed DM access", True)
    ], "the import is audited once, to the CLI's local user"


async def test_import_policies_turns_a_disabled_workspace_off(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `disabled` env policy turns the workspace's private messages off."""
    tenant, _ = await _tenant(db_session_factory)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        direct_message_policies={tenant.id: DirectMessagePolicy(mode="disabled")},
    )
    console, _ = _console()
    await dm_import_policies(
        rt=_runtime(db_session_factory, [], settings), console=console, dry_run=False
    )
    async with db_session_factory() as session:
        assert not await dm_enabled(session, tenant_id=tenant.id), "private messages are off"


async def test_import_policies_writes_off_on_a_default_off_workspace(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A default-off Discord workspace is still set off, so agent-sent messages stay blocked."""
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="g-default-off")
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        direct_message_policies={tenant.id: DirectMessagePolicy(mode="disabled")},
    )
    console, _ = _console()
    await dm_import_policies(
        rt=_runtime(db_session_factory, [], settings), console=console, dry_run=False
    )
    async with db_session_factory() as session:
        assert await dm_turned_off(session, tenant_id=tenant.id), "off is written, not defaulted"

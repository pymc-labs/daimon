"""`routines check-roles` lists owners who fail a role-gated agent's run-time check."""

from io import StringIO
from types import SimpleNamespace

import httpx
import pytest
from daimon.adapters.cli.commands.routines import run_check_roles
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.errors import UserFacingError
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter
from mux.testing.drivers.anthropic.ma import not_found_response
from rich.console import Console

from ..harness import build_cli_runtime


def _agents_router(*agents):
    """Serve each agent by id and in the tag lookup list; any other id is a 404."""
    router = MARouter()
    router.add_agent_list(*agents)
    for agent in agents:
        router.add("GET", rf"/v1/agents/{agent.id}", lambda _r, _m, a=agent: _ok(a))
    router.add("GET", r"/v1/agents/[^/]+", lambda _r, _m: not_found_response("gone"))
    return router


def _ok(agent):
    return httpx.Response(200, json=agent.model_dump(mode="json"))


_MEMBERS = {
    "1": Membership(is_member=True, role_ids=("123",)),  # holds the role
    "2": Membership(is_member=True, role_ids=(), is_admin=True),  # admin, no role
    "3": Membership(is_member=False),  # left the server
    "4": Membership(),  # the platform could not say
}


async def test_check_roles_lists_lapsed_and_unverified_owners_without_writing(
    db_session_factory,
):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="424242")
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(agent_rules={"gated": AgentRule(invoker_role_ids=("123",))}),
        )
        rows = {
            (owner, agent, enabled): await create_routine(
                session,
                tenant_id=tenant.id,
                created_by_user_id=owner,
                agent_id=f"ag-{agent}",
                agent_name=agent,
                cron_expr="0 9 * * *",
                timezone_="UTC",
                trigger_message="go",
                enabled=enabled,
            )
            for owner, agent, enabled in [
                ("1", "gated", True),
                ("2", "gated", True),
                ("3", "gated", True),
                ("4", "gated", True),
                ("2", "open", True),
                ("2", "gated", False),
            ]
        }
    router = _agents_router(
        *(
            ma_agent(id=f"ag-{n}", name=n, metadata={MA_METADATA_KEY_NAME: n}, tenant_id=tenant.id)
            for n in ("gated", "open")
        ),
    )
    rt = build_cli_runtime(db_session_factory, router=router, settings=SimpleNamespace())
    asked: list[str] = []

    async def fetch(_tenant_id, _guild_id, user_id):
        asked.append(user_id)
        return _MEMBERS[user_id]

    output = StringIO()
    await run_check_roles(
        rt=rt,
        console=Console(file=output, width=200),
        tenant_id=tenant.id,
        resolver=CachedMembershipResolver(fetch),
    )
    lines = output.getvalue().splitlines()
    assert sorted(set(asked)) == ["1", "2", "3", "4"], "only owners of enabled gated routines"
    lapsed, left = rows["2", "gated", True].id, rows["3", "gated", True].id
    unreadable = rows["4", "gated", True].id
    assert any(ln.startswith(f"{lapsed} owner=2 agent=gated: owner lacks a role") for ln in lines)
    assert any(ln.startswith(f"{left} owner=3") and "refused" in ln for ln in lines)
    assert any(
        ln.startswith(f"{unreadable} owner=4") and "could not be read: retried each run" in ln
        for ln in lines
    ), "an unreadable membership is retried, not refused"
    assert not any("owner=4" in ln and "refused" in ln for ln in lines)
    assert not any("owner=1" in ln or "agent=open" in ln for ln in lines)
    assert lines[-1] == "3 of 4 role-gated routines need attention."
    async with db_session_factory() as session:
        again = await get_routine(session, lapsed, tenant_id=tenant.id)
    assert again is not None and again.enabled and again.last_error is None, "read only"


async def test_check_roles_needs_a_discord_tenant(db_session_factory):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
    rt = build_cli_runtime(db_session_factory, settings=SimpleNamespace())
    with pytest.raises(UserFacingError, match="Discord"):
        await run_check_roles(
            rt=rt,
            console=Console(file=StringIO()),
            tenant_id=tenant.id,
            resolver=CachedMembershipResolver(lambda *_a: Membership()),  # type: ignore[arg-type,return-value]
        )


async def _fetch_two(*_args):
    return _MEMBERS["2"]


@pytest.mark.parametrize("by_id", [True, False], ids=["saved-agent-id", "healed-by-name"])
async def test_check_roles_lists_a_role_gate_added_by_an_agent_alias(db_session_factory, by_id):
    """A rule on the agent's own name gates a routine saved under another name."""
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="424242")
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(
                agent_rules={"Reviewer": AgentRule(invoker_role_ids=("123",))}
            ),
        )
        row = await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id="2",
            agent_id="ag-old" if not by_id else "ag-alias",
            agent_name="plain",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="go",
        )
    agent = ma_agent(
        id="ag-alias",
        name="Reviewer",
        metadata={MA_METADATA_KEY_NAME: "plain", MA_METADATA_KEY_TENANT: str(tenant.id)},
    )
    rt = build_cli_runtime(
        db_session_factory,
        router=_agents_router(agent),
        settings=SimpleNamespace(),
    )
    output = StringIO()
    await run_check_roles(
        rt=rt,
        console=Console(file=output, width=200),
        tenant_id=tenant.id,
        resolver=CachedMembershipResolver(_fetch_two),  # type: ignore[arg-type,return-value]
    )
    lines = output.getvalue().splitlines()
    assert any(
        ln.startswith(f"{row.id} owner=2 agent=plain: owner lacks a role") for ln in lines
    ), "the alias's gate is listed"
    assert lines[-1] == "1 of 1 role-gated routines need attention."

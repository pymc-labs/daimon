"""`routines check-roles` lists owners who fail a role-gated agent's run-time check."""

from io import StringIO
from types import SimpleNamespace

import pytest
from daimon.adapters.cli.commands.routines import run_check_roles
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.errors import UserFacingError
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_tenant
from rich.console import Console

from ..harness import build_cli_runtime

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
                agent_id="ag",
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
    rt = build_cli_runtime(db_session_factory, settings=SimpleNamespace())
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

"""`set_routine_sandbox_tools`: a current human admin's per-routine opt-in."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import daimon.adapters.mcp.tools.routines as _routines_mod
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.config import McpSettings
from daimon.core.membership import Membership
from daimon.core.routine_sandbox import SandboxPinUnavailable
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role, RoutineRow, SandboxPin
from daimon.core.stores.routines import create_routine, get_routine, sandbox_tools_history
from daimon.testing.factories import make_tenant
from daimon.testing.membership import FixtureMembershipResolver
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_set_impl = _routines_mod._set_routine_sandbox_tools_impl  # pyright: ignore[reportPrivateUsage]
_get_impl = _routines_mod._get_routine_impl  # pyright: ignore[reportPrivateUsage]
_list_impl = _routines_mod._list_routines_impl  # pyright: ignore[reportPrivateUsage]

ADMIN = "admin-1"
PIN = SandboxPin(
    agent_id="ag",
    agent_version=3,
    environment_id="env_1",
    environment_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
)


@pytest.fixture(autouse=True)
def _current_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The routine's agent and environment, as Managed Agents would report them."""
    monkeypatch.setattr(_routines_mod, "current_pin", AsyncMock(return_value=PIN))


OWNER = "owner-1"


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession], resolver: FixtureMembershipResolver
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(),  # type: ignore[arg-type]
        settings=MagicMock(mcp=McpSettings()),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
        membership_resolver=resolver,
    )


def _resolver(*, admin_is_admin: bool = True) -> FixtureMembershipResolver:
    resolver = FixtureMembershipResolver(ADMIN, OWNER)
    resolver.members[ADMIN] = Membership(is_member=True, is_admin=admin_is_admin)
    return resolver


def _auth(
    tenant_id: uuid.UUID,
    user_id: str,
    *,
    is_admin: bool,
    **overrides: Any,
) -> AuthIdentity:
    """A person's own Discord token (it carries a jti), with `overrides` applied."""
    fields: dict[str, Any] = {
        "account_id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "role": Role.ADMIN if is_admin else Role.USER,
        "platform": "discord",
        "external_id": "g_sandbox",
        "platform_user_id": user_id,
        "is_admin": is_admin,
        "token_jti": uuid.uuid4(),
        "token_kind": "cli",
        **overrides,
    }
    return AuthIdentity(**fields)


async def _setup(
    db_session: AsyncSession,
) -> tuple[uuid.UUID, RoutineRow, RoutineRow]:
    tenant = await make_tenant(db_session, workspace_id="g_sandbox")
    rows = [
        await create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id=OWNER,
            agent_id="ag",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message=f"nightly {n}",
        )
        for n in range(2)
    ]
    await db_session.commit()
    return tenant.id, rows[0], rows[1]


async def test_admin_opts_one_routine_in_and_status_shows_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, chosen, other = await _setup(db_session)
    runtime = _runtime(committing_sessionmaker, _resolver())
    admin = _auth(tenant_id, ADMIN, is_admin=True)

    result = await _set_impl(runtime, admin, routine_id=chosen.id, enabled=True)

    assert result.sandbox_tools_enabled is True
    assert (result.sandbox_agent_version, result.sandbox_environment_id) == (3, "env_1")
    assert result.sandbox_tools_set_by == str(admin.account_id)
    shown = await _get_impl(runtime, admin, routine_id=chosen.id)
    assert shown.sandbox_tools_enabled is True
    listed = {row.id: row.sandbox_tools_enabled for row in await _list_impl(runtime, admin)}
    assert listed == {chosen.id: True, other.id: False}
    async with committing_sessionmaker() as session:
        [entry] = await sandbox_tools_history(session, chosen.id, tenant_id=tenant_id)
    assert (entry.set_by, entry.via, entry.after_enabled) == (str(admin.account_id), "mcp", True)

    switched_off = await _set_impl(runtime, admin, routine_id=chosen.id, enabled=False)
    assert switched_off.sandbox_tools_enabled is False


PERSON_ONLY = "does not accept an agent"
ADMIN_ONLY = "owner cannot"


@pytest.mark.parametrize(
    ("user_id", "claims_admin", "live_admin", "overrides", "message"),
    [
        (OWNER, False, True, {}, ADMIN_ONLY),
        (ADMIN, True, False, {}, ADMIN_ONLY),
        (ADMIN, True, True, {"chat_agent_id": uuid.uuid4()}, PERSON_ONLY),
        (ADMIN, True, True, {"agent_id": uuid.uuid4()}, PERSON_ONLY),
        # A legacy vault credential from before the sweep carries no jti, even
        # when it was minted for an admin who is still an admin.
        (ADMIN, True, True, {"token_jti": None}, PERSON_ONLY),
        (ADMIN, True, True, {"token_kind": "operator"}, PERSON_ONLY),
        # No live check exists off Discord, so the stored admin role is not enough.
        (ADMIN, True, True, {"platform": "slack"}, ADMIN_ONLY),
        (ADMIN, True, True, {"platform": "teams"}, ADMIN_ONLY),
        (ADMIN, True, True, {"platform_user_id": None}, ADMIN_ONLY),
    ],
    ids=[
        "owner_not_admin",
        "demoted_admin",
        "agent_in_conversation",
        "agent_key",
        "legacy_jti_less_token",
        "operator_token",
        "slack_admin",
        "teams_admin",
        "no_platform_user",
    ],
)
async def test_everyone_but_a_current_discord_admin_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    user_id: str,
    claims_admin: bool,
    live_admin: bool,
    overrides: dict[str, Any],
    message: str,
) -> None:
    tenant_id, chosen, _ = await _setup(db_session)
    runtime = _runtime(committing_sessionmaker, _resolver(admin_is_admin=live_admin))
    caller = _auth(tenant_id, user_id, is_admin=claims_admin, **overrides)

    with pytest.raises(ToolError, match=message):
        await _set_impl(runtime, caller, routine_id=chosen.id, enabled=True)

    async with committing_sessionmaker() as session:
        after = await get_routine(session, chosen.id, tenant_id=tenant_id)
        assert await sandbox_tools_history(session, chosen.id, tenant_id=tenant_id) == []
    assert after is not None and after.sandbox_tools_enabled is False


async def test_unknown_routine_reads_as_not_found(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, _, _ = await _setup(db_session)
    runtime = _runtime(committing_sessionmaker, _resolver())

    with pytest.raises(ToolError, match="routine not found"):
        await _set_impl(
            runtime, _auth(tenant_id, ADMIN, is_admin=True), routine_id=uuid.uuid4(), enabled=True
        )


async def test_an_unreadable_agent_or_environment_refuses_the_opt_in(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, chosen, _ = await _setup(db_session)
    runtime = _runtime(committing_sessionmaker, _resolver())
    monkeypatch.setattr(
        _routines_mod, "current_pin", AsyncMock(side_effect=SandboxPinUnavailable("gone"))
    )

    with pytest.raises(ToolError, match="Can't read the routine's agent version"):
        await _set_impl(
            runtime, _auth(tenant_id, ADMIN, is_admin=True), routine_id=chosen.id, enabled=True
        )

    async with committing_sessionmaker() as session:
        after = await get_routine(session, chosen.id, tenant_id=tenant_id)
    assert after is not None and after.sandbox_tools_enabled is False

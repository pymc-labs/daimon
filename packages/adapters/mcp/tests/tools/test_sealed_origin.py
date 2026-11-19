"""The inside-only rule at the MCP boundary: who counts as inside, and who never does."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware import call_origin
from daimon.adapters.mcp.tools._authz_facts import CALL_ORIGIN, mcp_subject
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, authorize

from ..harness import auth_identity

SEALED = TenantAccessPolicy(channel_rules={"c-seal": ChannelRule(readers="own", writers="own")})
HERE = Place(channel_id="c-seal", parent_channel_id="c-seal")


def _auth(*, kind: str | None = None, session: str | None = "s1") -> AuthIdentity:
    return auth_identity(
        uuid.uuid4(),
        uuid.uuid4(),
        is_platform_admin=False,
        token_kind=kind,
        scopes=frozenset({"channels:write"}),
        platform="discord",
        platform_user_id="u1",
        chat_session_id=session,
    )


def test_an_operator_token_through_mcp_is_refused_on_a_sealed_channel() -> None:
    """Workspace admins can get operator tokens from a panel; only the local CLI is exempt."""
    auth = _auth(kind="operator")
    subject = mcp_subject(auth, is_admin=True)
    assert not subject.operator
    decision = authorize(
        SEALED,
        subject=subject,
        action=Action.CONFIGURE,
        agent=AgentRef.of("acme"),
        place=HERE,
    )
    assert not decision and decision.reason == "sealed_channel"


def _never(*_: object) -> bool:
    return False


def _origin(*, attended: bool, thread: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(parent_channel_id="c-seal", thread_id=thread, attended=attended)


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [True, False], ids=["named", "fallback"])
async def test_only_an_attended_turn_proves_an_origin(
    monkeypatch: pytest.MonkeyPatch, named: bool
) -> None:
    async def verified(*_: object) -> object:
        return _origin(attended=False) if named else None

    async def running(*_: object) -> list[object]:
        return [] if named else [_origin(attended=False)]

    monkeypatch.setattr(call_origin, "get_verified_origin", verified)
    monkeypatch.setattr(call_origin, "list_running_origins", running)
    assert await call_origin.call_origin_ids(object(), _auth(), "x") == frozenset()  # pyright: ignore[reportArgumentType]

    async def verified_ok(*_: object) -> object:
        return _origin(attended=True) if named else None

    async def running_ok(*_: object) -> list[object]:
        return [] if named else [_origin(attended=True)]

    monkeypatch.setattr(call_origin, "get_verified_origin", verified_ok)
    monkeypatch.setattr(call_origin, "list_running_origins", running_ok)
    ids = await call_origin.call_origin_ids(object(), _auth(), "x")  # pyright: ignore[reportArgumentType]
    assert "c-seal" in ids


@pytest.mark.asyncio
async def test_the_fallback_keeps_a_held_credential_inside_its_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def none(*_: object) -> None:
        return None

    async def running(*_: object) -> list[object]:
        return [_origin(attended=True)]

    monkeypatch.setattr(call_origin, "get_verified_origin", none)
    monkeypatch.setattr(call_origin, "list_running_origins", running)
    held = _auth()
    object.__setattr__(held, "hold", SimpleNamespace(contains=_never))
    assert await call_origin.call_origin_ids(object(), held, None) == frozenset()  # pyright: ignore[reportArgumentType]


def test_the_call_origin_reaches_the_subject_but_never_an_agent_key() -> None:
    token = CALL_ORIGIN.set(frozenset({"c-seal"}))
    try:
        assert mcp_subject(_auth(), is_admin=True).inside == {"c-seal"}
    finally:
        CALL_ORIGIN.reset(token)

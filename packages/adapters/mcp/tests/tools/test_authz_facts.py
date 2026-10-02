"""`mcp_subject` and `mcp_place`: how an MCP caller and its place reach `authorize`."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._authz_facts import mcp_place, mcp_subject
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, SessionFacts, Surface, authorize
from daimon.core.stores.domain import Role

_POLICY = TenantAccessPolicy(
    agent_channel_pins={"acme": ("c-1",), "beta": ("c-2",)}, sealed_channel_ids=("c-2",)
)


def _caller(*, agent_key: bool, bound: str | None) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        platform_user_id="u1",
        agent_id=uuid.uuid4() if agent_key else None,
        bound_channel_id=bound,
    )


@pytest.mark.parametrize(
    ("agent_key", "bound", "expected"),
    [
        (True, "c-1", Place(channel_id="c-1", parent_channel_id="c-1")),
        (True, None, Place()),
        (False, "c-1", Place()),
    ],
    ids=["bound-key", "unbound-key", "not-an-agent-key"],
)
def test_mcp_place_is_only_a_bound_agent_keys_channel(
    agent_key: bool, bound: str | None, expected: Place
) -> None:
    assert mcp_place(_caller(agent_key=agent_key, bound=bound)) == expected, (
        "only an agent key's own binding places an MCP call in a channel"
    )


def test_a_bound_key_holds_no_channel_admin_grants_even_if_its_account_does() -> None:
    """The binding narrows where the key runs; it never lends it the minter's grants."""
    auth = replace(
        _caller(agent_key=True, bound="c-1"), administered_channel_ids=frozenset({"c-1", "c-2"})
    )
    subject = mcp_subject(auth)

    assert subject.via_agent_key, "an agent key is marked as one"
    assert subject.administered_channel_ids == frozenset(), "and holds no grants"
    assert not authorize(
        _POLICY,
        subject=subject,
        action=Action.CONFIGURE,
        surface=Surface.CONFIG,
        agent=AgentRef.of("beta"),
        place=mcp_place(auth),
    ), "a grant on c-2 never lets a key bound to c-1 configure c-2's pinned agent"
    assert not authorize(
        _POLICY,
        subject=subject,
        action=Action.RUN_AGENT,
        surface=Surface.AGENT_CHAT,
        agent=AgentRef.of("beta"),
        place=mcp_place(auth),
    ), "nor run it outside the key's own channel"
    assert not authorize(
        _POLICY,
        subject=subject,
        action=Action.READ_SESSION,
        surface=Surface.HUB,
        origin_channel_ids=frozenset({"c-1"}),
        session=SessionFacts(channel="c-2", seal_ids=frozenset({"c-2"}), owned=False),
    ), "nor read another member's sealed c-2 conversation"
    assert authorize(
        _POLICY,
        subject=subject,
        action=Action.RUN_AGENT,
        surface=Surface.AGENT_CHAT,
        agent=AgentRef.of("acme"),
        place=mcp_place(auth),
    ), "inside its own channel the pin admits it"

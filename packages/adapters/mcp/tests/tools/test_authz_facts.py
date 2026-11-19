"""`mcp_subject` and `mcp_place`: how an MCP caller and its place reach `authorize`."""

from __future__ import annotations

import uuid

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._authz_facts import mcp_place
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Place
from daimon.core.stores.domain import Role

from ..harness import auth_identity

_POLICY = TenantAccessPolicy(
    agent_channel_pins={"acme": ("c-1",), "beta": ("c-2",)}, sealed_channel_ids=("c-2",)
)


def _caller(*, agent_key: bool, bound: str | None) -> AuthIdentity:
    return auth_identity(
        uuid.uuid4(),
        uuid.uuid4(),
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

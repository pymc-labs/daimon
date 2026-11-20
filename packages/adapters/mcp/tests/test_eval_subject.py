"""Who MCP callers are to evals: external agent keys and clients are never admins."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.evals import eval_subject
from daimon.core.stores.domain import Role
from daimon.core.turn_hold import TurnHold

from .harness import auth_identity

_RUNTIME = cast(McpRuntime, None)  # the platform-less callers below need no group lookup


@pytest.mark.parametrize(
    ("fields", "role"),
    [
        ({}, "admin"),
        ({"agent_id": uuid.uuid4()}, "member"),
        ({"token_kind": "agent"}, "member"),
        ({"foreign_home_id": "other-org"}, "visitor"),
        ({"hold": TurnHold(platform="slack", channel_id="C1", client=True)}, "visitor"),
    ],
)
def test_eval_subject_when_admin_key_is_not_a_person_admin_then_not_admin(
    fields: dict[str, Any], role: str
) -> None:
    auth = auth_identity(uuid.uuid4(), uuid.uuid4(), Role.ADMIN, **fields)
    assert eval_subject(_RUNTIME, auth).role == role, fields

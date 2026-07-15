from __future__ import annotations

import uuid
from typing import Any

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.tools._ctx import _auth
from fastmcp.exceptions import ToolError


class _FakeCtx:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state

    async def get_state(self, key: str) -> Any:  # noqa: ANN401  # mirrors fastmcp.Context.get_state
        return self._state.get(key)


async def test_auth_returns_identity_when_middleware_seeded_it() -> None:
    identity = AuthIdentity(account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN)
    ctx = _FakeCtx({"auth": identity})
    got = await _auth(ctx)  # type: ignore[arg-type]
    assert got is identity, "should return the seeded identity"


async def test_auth_raises_tool_error_when_state_missing() -> None:
    ctx = _FakeCtx({})
    with pytest.raises(ToolError, match="missing auth"):
        await _auth(ctx)  # type: ignore[arg-type]

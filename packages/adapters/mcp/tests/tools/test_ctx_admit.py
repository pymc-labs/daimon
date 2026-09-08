"""_admit is the identity-taking core of the admission gate."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._ctx import _admit  # pyright: ignore[reportPrivateUsage]
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError

pytestmark = pytest.mark.asyncio


async def test_admit_denies_when_over_balance() -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, platform_user_id="u1"
    )
    with (
        patch("daimon.adapters.mcp.tools._ctx.is_over_balance", new=AsyncMock(return_value=True)),
        pytest.raises(ToolError, match="credit is depleted"),
    ):
        await _admit(auth, sessionmaker=AsyncMock(), billing_config=None, tool_name="ask")


async def test_admit_skips_billing_for_internal_identity() -> None:
    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER)
    with patch(
        "daimon.adapters.mcp.tools._ctx.is_over_balance",
        new=AsyncMock(side_effect=AssertionError("must not check")),
    ):
        result = await _admit(auth, sessionmaker=AsyncMock(), billing_config=None, tool_name="ask")
    assert result is auth

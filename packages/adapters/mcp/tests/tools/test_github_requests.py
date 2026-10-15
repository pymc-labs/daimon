"""GitHub access requests: where they are offered."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import github_requests as requests_module
from daimon.core.stores.domain import Role


async def test_teams_request_points_to_connect_github_not_another_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teams has no admin request cards yet; Connect GitHub works there instead."""
    origin = SimpleNamespace(platform="teams", is_external=False)
    monkeypatch.setattr(requests_module, "require_turn_origin", AsyncMock(return_value=origin))
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="teams",
        platform_user_id="aad-member",
    )
    result = await requests_module.request_github_access_impl(
        SimpleNamespace(),  # type: ignore[arg-type]
        auth,
        origin_context_id=str(uuid.uuid4()),
        repo_name="owner/repo",
        required_ability="write",
        remaining_work="Review the open pull request",
    )
    assert result.status == "unavailable", "Teams should not queue an admin request"
    assert "connect GitHub" in result.message, "the reply should point to Connect GitHub"
    assert "Discord" not in result.message and "Slack" not in result.message, (
        "a Teams user should not be sent to another platform"
    )

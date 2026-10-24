"""The MCP middleware on an OpenAI/Gemini turn: which gated tools reach Daimon's own card."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.core.stores.domain import Role
from daimon.core.tool_approvals import args_digest, pending_call
from fastmcp.exceptions import ToolError


def _middleware() -> IdentityMiddleware:
    unused = MagicMock()
    return IdentityMiddleware(
        subject_resolver=unused,
        tenant_resolver=unused,
        role_resolver=unused,
        agent_id_resolver=unused,
        is_admin_resolver=unused,
        internal_resolver=unused,
        sessionmaker=MagicMock(),
    )


def _provider_turn() -> AuthIdentity:
    return AuthIdentity(
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
        no_confirmation_surface=True,
    )


async def _call(name: str, arguments: dict[str, object]):
    seen: list[object] = []

    async def call_next(_context):
        seen.append(pending_call.get())
        return "ran"

    context = SimpleNamespace(
        method="tools/call", message=SimpleNamespace(name=name, arguments=arguments)
    )
    result = await _middleware()._dispatch_for_backend(context, call_next, _provider_turn())  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    return result, seen


async def test_a_publish_call_records_its_exact_call_for_the_card_gate():
    arguments = {"title": "Q3", "origin_context_id": "o"}
    result, seen = await _call("publish_report", arguments)
    assert result == "ran"
    assert seen == [("publish_report", args_digest("publish_report", arguments), arguments)]
    assert pending_call.get() is None, "recorded for that one call only"


async def test_an_add_skill_preview_records_nothing():
    result, seen = await _call("add_skill", {"name": "notes"})
    assert result == "ran" and seen == [None]


async def test_a_gated_tool_daimon_cant_card_is_still_refused():
    with pytest.raises(ToolError, match="confirmation card"):
        await _call("microsoft_send", {"action": "send"})

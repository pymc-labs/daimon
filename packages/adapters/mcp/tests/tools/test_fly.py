"""Fly tool admission refuses forged/unattended execution before any provider access."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import fly
from daimon.core.stores.domain import Role, TurnOriginRow
from fastmcp.exceptions import ToolError


@pytest.mark.parametrize(
    "mode",
    [
        "routine",
        "worker",
        "agent",
        "missing_session",
        "no_chat_agent",
        "unattended",
        "nonhuman",
        "missing_human",
        "forged",
        "valid",
    ],
)
async def test_attended_signed_owner_admission(monkeypatch, mode):
    origin = TurnOriginRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        attended=True,
        human_invocation=True,
        platform="discord",
        chat_session_id="signed",
        parent_channel_id="parent",
        thread_id="thread",
        responder_ma_agent_id="agent",
        responder_name="Daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    kwargs = dict(
        tenant_id=origin.tenant_id,
        account_id=origin.account_id,
        role=Role.USER,
        chat_agent_id=uuid.uuid4(),
        chat_session_id="signed",
        platform_user_id="owner",
        platform="discord",
    )
    if mode in {"routine", "worker", "agent"}:
        kwargs[{"routine": "routine_id", "worker": "execution_id", "agent": "agent_id"}[mode]] = (
            uuid.uuid4()
        )
    if mode == "missing_session":
        kwargs["chat_session_id"] = None
    if mode == "no_chat_agent":
        kwargs["chat_agent_id"] = None
    if mode == "unattended":
        origin = origin.model_copy(update={"attended": False})
    if mode in {"nonhuman", "missing_human"}:
        origin = origin.model_copy(
            update={"human_invocation": False if mode == "nonhuman" else None}
        )
    monkeypatch.setattr(fly, "_auth", AsyncMock(return_value=AuthIdentity(**kwargs)))
    verifier = AsyncMock(return_value=origin)
    if mode == "forged":
        verifier.side_effect = ToolError("Origin is forged")
    monkeypatch.setattr(fly, "require_turn_origin", verifier)
    if mode == "valid":
        _, caller = await fly._caller(SimpleNamespace(), None, str(origin.id))
        assert caller.account_id == origin.account_id and not caller.admin
    else:
        with pytest.raises(ToolError):
            await fly._caller(SimpleNamespace(), None, str(origin.id))
    if mode in {"routine", "worker", "agent", "missing_session", "no_chat_agent"}:
        verifier.assert_not_called()


@pytest.mark.parametrize("mode", ["approved", "unapproved", "changed", "foreign_tool", "not_gated"])
async def test_exact_approval_arguments_and_session_key(monkeypatch, mode):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def db():
        yield None

    runtime = SimpleNamespace(session_factory=db, client=None)
    auth = AuthIdentity(
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        chat_session_id="chat",
    )
    origin = SimpleNamespace(
        platform="discord",
        thread_id="thread",
        responder_ma_agent_id="agent",
        parent_channel_id="parent",
    )
    gap = AsyncMock(return_value="session_not_gated" if mode == "not_gated" else None)
    monkeypatch.setattr(fly._session_gate, "session_card_gap", gap)
    monkeypatch.setattr(
        fly,
        "get_live_thread_session",
        AsyncMock(return_value=SimpleNamespace(ma_session_id="parent-session")),
    )
    monkeypatch.setattr(
        fly,
        "retrieve_session",
        AsyncMock(
            return_value=SimpleNamespace(
                id="parent-session", agent=SimpleNamespace(id="agent"), metadata={}
            )
        ),
    )
    args = {"template": "mmm", "origin_context_id": "origin", "region": "iad", "ttl_hours": None}
    event_args = {"template": "mmm", "origin_context_id": "origin"}
    if mode == "changed":
        event_args["region"] = "lhr"
    events = [
        SimpleNamespace(
            type="agent.mcp_tool_use",
            name="fly_launch_session",
            mcp_server_name="foreign" if mode == "foreign_tool" else fly.DAIMON_SERVER_NAME,
            input=event_args,
            id="tool-call",
        )
    ]
    if mode != "unapproved":
        events.append(
            SimpleNamespace(type="user.tool_confirmation", result="allow", tool_use_id="tool-call")
        )
    monkeypatch.setattr(
        fly, "LegacyTurnIO", lambda *a, **k: SimpleNamespace(replay=AsyncMock(return_value=events))
    )
    if mode == "approved":
        key, metadata = await fly._approved(
            runtime, auth, origin, "fly_launch_session", args, {"region": "iad", "ttl_hours": None}
        )
        assert key == "parent-session:tool-call"
        assert metadata["daimon_thread"] == "thread"
    else:
        with pytest.raises(ToolError):
            await fly._approved(
                runtime,
                auth,
                origin,
                "fly_launch_session",
                args,
                {"region": "iad", "ttl_hours": None},
            )

"""Compute tool admission refuses forged/unattended execution before any provider access."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import compute
from daimon.core.compute_thin_mcp import MAX_READ_BYTES
from daimon.core.config import ComputeSettings
from daimon.core.stores.domain import Role, TurnOriginRow
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..harness import auth_identity


@pytest.mark.parametrize(
    "mode",
    [
        "client",
        "foreign_sender",
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
    if mode == "client":
        kwargs["foreign_home_id"] = "other-organisation"
    if mode == "foreign_sender":
        origin = origin.model_copy(update={"sender_foreign": True})
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
    monkeypatch.setattr(compute, "_auth", AsyncMock(return_value=AuthIdentity(**kwargs)))
    verifier = AsyncMock(return_value=origin)
    if mode == "forged":
        verifier.side_effect = ToolError("Origin is forged")
    monkeypatch.setattr(compute, "require_turn_origin", verifier)
    if mode == "valid":
        _, caller = await compute._caller(SimpleNamespace(), None, str(origin.id))
        assert caller.account_id == origin.account_id and not caller.admin
    else:
        with pytest.raises(ToolError):
            await compute._caller(SimpleNamespace(), None, str(origin.id))
    if mode in {"client", "routine", "worker", "agent", "missing_session", "no_chat_agent"}:
        verifier.assert_not_called()


@pytest.mark.parametrize("mode", ["approved", "unapproved", "changed", "foreign_tool", "not_gated"])
async def test_exact_approval_arguments_and_session_key(monkeypatch, mode):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def db():
        yield None

    runtime = SimpleNamespace(session_factory=db, client=None)
    auth = auth_identity(
        uuid.uuid4(), uuid.uuid4(), role=Role.USER, platform="discord", chat_session_id="chat"
    )
    origin = SimpleNamespace(
        platform="discord",
        thread_id="thread",
        responder_ma_agent_id="agent",
        parent_channel_id="parent",
    )
    gap = AsyncMock(return_value="session_not_gated" if mode == "not_gated" else None)
    monkeypatch.setattr(compute._session_gate, "session_card_gap", gap)
    monkeypatch.setattr(
        compute,
        "get_live_thread_session",
        AsyncMock(return_value=SimpleNamespace(ma_session_id="parent-session")),
    )
    monkeypatch.setattr(
        compute,
        "retrieve_session",
        AsyncMock(
            return_value=SimpleNamespace(
                id="parent-session", agent=SimpleNamespace(id="agent"), metadata={}
            )
        ),
    )
    args = {
        "template": "mmm",
        "origin_context_id": "origin",
        "region": "us-central1",
        "ttl_hours": None,
    }
    event_args = {"template": "mmm", "origin_context_id": "origin", "region": "us-central1"}
    if mode == "changed":
        event_args["region"] = "europe-west4"
    events = [
        SimpleNamespace(
            type="agent.mcp_tool_use",
            name="compute_launch_session",
            mcp_server_name="foreign" if mode == "foreign_tool" else compute.DAIMON_SERVER_NAME,
            input=event_args,
            id="tool-call",
        )
    ]
    if mode != "unapproved":
        events.append(
            SimpleNamespace(type="user.tool_confirmation", result="allow", tool_use_id="tool-call")
        )
    monkeypatch.setattr(
        compute, "turn_io", lambda *a, **k: SimpleNamespace(replay=AsyncMock(return_value=events))
    )
    if mode == "approved":
        key, metadata = await compute._approved(
            runtime, auth, origin, "compute_launch_session", args, {"ttl_hours": None}
        )
        assert key == "parent-session:tool-call"
        assert metadata["daimon_thread"] == "thread"
    else:
        with pytest.raises(ToolError):
            await compute._approved(
                runtime,
                auth,
                origin,
                "compute_launch_session",
                args,
                {"ttl_hours": None},
            )


@pytest.mark.parametrize(
    "mode", ["same", "proxied", "proxied_other_tool", "changed", "foreign_tool", "none"]
)
async def test_background_command_key_is_the_provider_tool_use(monkeypatch, mode):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def db():
        yield None

    runtime = SimpleNamespace(session_factory=db, client=None)
    auth = auth_identity(
        uuid.uuid4(), uuid.uuid4(), role=Role.USER, platform="discord", chat_session_id="chat"
    )
    origin = SimpleNamespace(
        platform="discord",
        thread_id="thread",
        responder_ma_agent_id="agent",
        parent_channel_id="parent",
    )
    # Inline bash has no approval card; a background start must not demand one.
    gap = AsyncMock(return_value="session_not_gated")
    monkeypatch.setattr(compute._session_gate, "session_card_gap", gap)
    monkeypatch.setattr(
        compute,
        "get_live_thread_session",
        AsyncMock(return_value=SimpleNamespace(ma_session_id="parent-session")),
    )
    monkeypatch.setattr(
        compute,
        "retrieve_session",
        AsyncMock(
            return_value=SimpleNamespace(
                id="parent-session", agent=SimpleNamespace(id="agent"), metadata={}
            )
        ),
    )
    args = {
        "session_id": "s",
        "command": "python fit.py",
        "origin_context_id": "origin",
        "timeout": 40000,
        "run_in_background": True,
        "timeout_minutes": None,
    }
    defaults = {"timeout": 40000, "run_in_background": False, "timeout_minutes": None}
    event_input = {
        "session_id": "s",
        "command": "python fit.py" if mode != "changed" else "python other.py",
        "origin_context_id": "origin",
        "run_in_background": True,
    }
    events = [
        SimpleNamespace(
            type="agent.mcp_tool_use",
            name="compute_run_bash",
            mcp_server_name="foreign" if mode == "foreign_tool" else compute.DAIMON_SERVER_NAME,
            input={**event_input, "run_in_background": False},
            id="earlier-inline",
        ),
        SimpleNamespace(
            type="agent.mcp_tool_use",
            name="compute_run_bash",
            mcp_server_name="foreign" if mode == "foreign_tool" else compute.DAIMON_SERVER_NAME,
            input=event_input,
            id="background-use",
        ),
    ]
    if mode == "none":
        events = []
    if mode.startswith("proxied"):
        # The agent reaches compute_run_bash through the search interface's proxy.
        inner = "compute_read_file" if mode == "proxied_other_tool" else "compute_run_bash"
        events = [
            SimpleNamespace(
                type=e.type,
                name="call_tool",
                mcp_server_name=e.mcp_server_name,
                input={"name": inner, "arguments": e.input},
                id=e.id,
            )
            for e in events
        ]
    monkeypatch.setattr(
        compute, "turn_io", lambda *a, **k: SimpleNamespace(replay=AsyncMock(return_value=events))
    )
    if mode in {"same", "proxied"}:
        key, _ = await compute._approved(
            runtime, auth, origin, "compute_run_bash", args, defaults, approval=False
        )
        assert key == "parent-session:background-use"
        gap.assert_not_awaited()
    else:
        with pytest.raises(ToolError):
            await compute._approved(
                runtime, auth, origin, "compute_run_bash", args, defaults, approval=False
            )


def test_task_view_withholds_output_except_from_result():
    from daimon.core.compute_models import ComputeTask

    now = datetime.now(UTC)
    task = ComputeTask(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        owner_account_id=uuid.uuid4(),
        request_key="parent:use",
        command_preview="python fit.py",
        timeout_minutes=60,
        deadline_at=now,
        output_tail="secret output",
        output_path="/tmp/daimon-tasks/x.out",
        created_at=now,
    )
    status = compute._task_view(task, output=False)
    assert status["task_id"] == str(task.id) and "output_tail" not in status
    assert not {"tenant_id", "owner_account_id", "request_key"} & set(status)
    assert compute._task_view(task, output=True)["output_tail"] == "secret output"


async def test_read_limit_is_the_largest_chunk_the_session_answer_can_carry():
    """The tool, its message and its description all take the limit from the transport."""
    assert compute._read_arguments("/data/out.csv", 0, MAX_READ_BYTES)["limit"] == MAX_READ_BYTES
    for path, offset, limit in (
        ("/data/out.csv", 0, MAX_READ_BYTES + 1),
        ("/data/out.csv", 0, 0),
        ("/data/out.csv", -1, 1),
        ("relative", 0, 1),
        ("/nul\x00", 0, 1),
    ):
        with pytest.raises(ToolError, match=f"{MAX_READ_BYTES // 1024} KiB"):
            compute._read_arguments(path, offset, limit)
    mcp = FastMCP("test")
    runtime = SimpleNamespace(
        session_factory=None,
        settings=SimpleNamespace(compute=ComputeSettings()),
        compute_provider=None,
    )
    compute.register_compute_tools(mcp, runtime)
    description = (await mcp.get_tool("compute_read_file")).description
    assert f"at most {MAX_READ_BYTES // 1024} KiB" in description

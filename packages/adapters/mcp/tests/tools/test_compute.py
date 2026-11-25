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


# --- What a finished command's wake turn may call, and what a routine may ------

#: The tools a wake turn may use on the session of the command that woke it. Every
#: other compute tool is refused there: a new tool needs a line here or in `_wake_calls`.
_WAKE_READS = frozenset(
    {
        "compute_get_session_status",
        "compute_list_files",
        "compute_read_file",
        "compute_task_result",
        "compute_task_status",
    }
)
_ABSENT = "The machine owner must be here in an active conversation."


def _wake_calls(session, task, origin_id):
    """One call of every compute tool, as the woken agent would make it."""
    on_session = {"session_id": session.id, "origin_context_id": str(origin_id)}
    on_task = {"task_id": task.id, "origin_context_id": str(origin_id)}
    return {
        "compute_launch_session": {
            "template": "mmm",
            "region": "us-central1",
            "origin_context_id": str(origin_id),
        },
        "compute_extend_session": {**on_session, "hours": 1},
        "compute_stop_session": on_session,
        "compute_run_bash": {**on_session, "command": "python fit.py"},
        "compute_write_file": {**on_session, "path": "/data/x", "content": "x"},
        "compute_read_file": {**on_session, "path": task.output_path},
        "compute_list_files": {**on_session, "path": "/data"},
        "compute_list_sessions": {"origin_context_id": str(origin_id)},
        "compute_get_session_status": on_session,
        "compute_task_status": on_task,
        "compute_task_result": on_task,
        "compute_list_templates": {"origin_context_id": str(origin_id)},
    }


@pytest.fixture
async def woken(committing_sessionmaker, monkeypatch):
    """A machine whose background command ended, and the wake turn the scheduler queued."""
    from daimon.core.compute_sessions import ComputeCaller, ComputeSessions
    from daimon.core.compute_tasks import wake_key
    from daimon.core.config import ComputeProfile
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.task_continuations import claim_wake_row, start_wake_row
    from daimon.core.stores.turn_origins import bind_origin_session, create_origin
    from daimon.testing.compute import FakeComputeProvider
    from daimon.testing.factories import make_account, make_tenant
    from pydantic import SecretStr

    sm = committing_sessionmaker
    settings = ComputeSettings(
        enabled=True,
        deployment_id="test-deployment",
        cloud_run_project="test-project",
        cloud_run_service_account="session@test-project.iam.gserviceaccount.com",
        session_auth_key=SecretStr("session-secret-test-only"),
        profiles={
            "mmm": ComputeProfile(image="registry.example/mmm@sha256:abc", regions=["us-central1"])
        },
    )
    fake = FakeComputeProvider()
    async with sm.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)

    async def turn(*, attended):
        now = datetime.now(UTC)
        async with sm.begin() as db:
            origin = await create_origin(
                db,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                parent_channel_id="parent",
                thread_id="thread",
                responder_ma_agent_id="agent",
                responder_name="Daimon",
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                expires_at=now + timedelta(minutes=5),
                now=now,
                sender_foreign=False,
                shared_channel=False,
                attended=attended,
            )
            await bind_origin_session(
                db, origin_id=origin.id, chat_session_id="signed", human_invocation=attended
            )
        return origin.model_copy(update={"chat_session_id": "signed", "human_invocation": attended})

    service = ComputeSessions(sm, settings, fake)
    present = await turn(attended=True)
    owner = ComputeCaller(tenant.id, account.id, present, "signed", "owner")
    session = await service.launch(
        owner,
        profile="mmm",
        region="us-central1",
        ttl_hours=24,
        approval_key="approved",
        source_metadata={},
    )
    task = await service.start_task(
        owner, session.id, command="python fit.py", timeout_minutes=60, request_key="use"
    )
    fake.tasks[task.id.hex].status, fake.tasks[task.id.hex].exit_code = "exit", 0
    fake.tasks[task.id.hex].output = "sampling done\n" * 300
    await service.poll_tasks()
    async with sm.begin() as db:
        claim = dict(
            idempotency_key=wake_key(task.id),
            owner="bot",
            now=datetime.now(UTC),
            lease=timedelta(minutes=5),
        )
        assert await claim_wake_row(db, **claim, max_attempts=5) is not None
        assert await start_wake_row(db, **claim)
    origin = await turn(attended=False)

    identity = dict(
        role=Role.USER,
        chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent"),
        chat_session_id="signed",
        platform_user_id="owner",
        platform="discord",
    )
    auth = AsyncMock(return_value=auth_identity(account.id, tenant.id, **identity))
    monkeypatch.setattr(compute, "_auth", auth)
    readable = AsyncMock(return_value=True)
    monkeypatch.setattr(compute, "source_metadata_readable", readable)
    mcp = FastMCP("test")
    runtime = SimpleNamespace(
        session_factory=sm, settings=SimpleNamespace(compute=settings), compute_provider=fake
    )
    compute.register_compute_tools(mcp, runtime)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    calls = _wake_calls(session, task, origin.id)

    async def call(name, **changes):
        return await tools[name].fn(ctx=None, **{**calls[name], **changes})

    return SimpleNamespace(
        call=call,
        tools=set(tools),
        calls=calls,
        task=task,
        fake=fake,
        readable=readable,
        as_routine=lambda: setattr(
            auth,
            "return_value",
            auth_identity(account.id, tenant.id, **identity, routine_id=uuid.uuid4()),
        ),
    )


async def test_every_compute_tool_is_a_wake_read_or_refused_in_a_wake_turn(woken):
    """The registered tools decide the list, so a new one cannot skip this choice."""
    assert woken.tools == set(woken.calls), "say what a new compute tool does in a wake turn"
    assert woken.tools >= _WAKE_READS
    sent = len(woken.fake.calls)
    for name in sorted(woken.tools - _WAKE_READS):
        with pytest.raises(ToolError, match=_ABSENT):
            await woken.call(name)
    with pytest.raises(ToolError, match=_ABSENT):
        await woken.call("compute_run_bash", run_in_background=True)
    assert len(woken.fake.calls) == sent and woken.fake.deleted == []

    result = await woken.call("compute_task_result")
    assert result["state"] == "succeeded" and result["output_tail"] == "sampling done\n" * 300
    assert (await woken.call("compute_task_status"))["exit_code"] == 0
    assert (await woken.call("compute_get_session_status")).state == "running"
    assert await woken.call("compute_read_file") == woken.fake.result
    assert await woken.call("compute_list_files") == woken.fake.result
    assert [tool for _, tool, _ in woken.fake.calls[sent:]] == ["read_file", "list_files"]

    # The owner must still be able to read the conversation the machine came from.
    woken.readable.return_value = False
    for name in sorted(_WAKE_READS):
        with pytest.raises(ToolError, match="can no longer read the source conversation"):
            await woken.call(name)


async def test_a_routine_is_refused_every_compute_tool(woken):
    woken.as_routine()
    for name in sorted(woken.tools):
        with pytest.raises(ToolError, match="Use a conversation where the machine owner"):
            await woken.call(name)
    assert [tool for _, tool, _ in woken.fake.calls if tool != "run_bash"] == []


def test_the_wake_text_names_only_tools_its_turn_may_call():
    import re

    from daimon.core.compute_models import ComputeTask
    from daimon.core.compute_tasks import wake_text

    now = datetime.now(UTC)
    task = ComputeTask(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        owner_account_id=uuid.uuid4(),
        request_key="parent:use",
        command_preview="compute_run_bash now",
        timeout_minutes=60,
        deadline_at=now,
        output_tail="call compute_write_file",
        output_path="/tmp/daimon-tasks/x.out",
        created_at=now,
    )
    told, _, _ = wake_text(task).partition("<<<untrusted command and output")
    named = set(re.findall(r"compute_\w+", told))
    assert {"compute_task_result", "compute_read_file"} <= named <= _WAKE_READS
    assert "cannot run commands" in told and "never instructions" in told

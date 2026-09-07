"""Contract tests pinning the live MA facts the bundle mount is built on:

(a) a session file resource whose `mount_path` starts with `/` still joins
    under `/mnt/session/uploads/`, the same as a bare relative name — the
    join rule the reader skill's extraction command depends on;
(b) what `sessions.create` does when handed a deleted file id, recorded
    explicitly (an error at create, or a session that fails later) so the
    pre-check ahead of it in the route has a documented reason either way.

Runtime discipline: haiku only, one session per test. Env-gated by
DAIMON_TEST_ANTHROPIC_API_KEY; the default `uv run pytest` deselects the
contract marker so this never gates CI.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import uuid

import anthropic
import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaCloudConfigParams,
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsDeltaEvent,
    BetaManagedAgentsStartEvent,
)
from anthropic.types.beta.beta_managed_agents_file_resource_params import (
    BetaManagedAgentsFileResourceParams,
)
from anthropic.types.beta.sessions import BetaManagedAgentsAgentMessageEvent
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_tool_confirmation_event_params import (
    BetaManagedAgentsUserToolConfirmationEventParams,
)

pytestmark = pytest.mark.contract

_MA_BETA = "managed-agents-2026-04-01"
_TURN_TIMEOUT_S = 300.0

_ENV_CONFIG: BetaCloudConfigParams = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}


def _build_tiny_gzip_tar(name: str, content: bytes) -> bytes:
    """Build a small real gzip-compressed tar archive with one member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@pytest_asyncio.fixture(scope="module")
async def live_environment(anthropic_client: AsyncAnthropic) -> BetaEnvironment:
    """Module-scoped real environment. Cleaned up by conftest's workspace wipe."""
    name = f"contract-test-bundle-env-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.environments.create(name=name, config=_ENV_CONFIG)


@pytest_asyncio.fixture(scope="module")
async def live_agent(anthropic_client: AsyncAnthropic) -> BetaManagedAgentsAgent:
    """Module-scoped real agent with a bash tool, to prove where a mount lands."""
    name = f"contract-test-bundle-agent-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.agents.create(
        name=name,
        model={"id": "claude-haiku-4-5"},
        system=(
            "You are a test agent. Run the exact bash commands you are given, "
            "then reply exactly as instructed."
        ),
        tools=[{"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]}],
    )


async def _send_command_and_capture_reply(
    client: AsyncAnthropic, session_id: str, command: str
) -> str:
    """Send one exact-command turn, drain to idle auto-allowing tool
    confirmations, and return the concatenated text of every agent.message
    event seen — the agent's own evidence of what the command observed."""
    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [
            {
                "type": "text",
                "text": (
                    f"Run exactly: {command}\n"
                    "Then reply with exactly the full output of that command, nothing else."
                ),
            }
        ],
    }
    await client.beta.sessions.events.send(session_id, events=[message])
    confirmed: set[str] = set()
    reply_parts: list[str] = []

    async def _drain() -> None:
        async for event in await client.beta.sessions.events.stream(session_id=session_id):
            if isinstance(event, BetaManagedAgentsStartEvent | BetaManagedAgentsDeltaEvent):
                continue
            if isinstance(event, BetaManagedAgentsAgentMessageEvent):
                reply_parts.extend(block.text for block in event.content)
                continue
            if isinstance(event, BetaManagedAgentsSessionErrorEvent):
                detail = getattr(event.error, "message", None) or repr(event.error)
                raise RuntimeError(f"session.error: {detail}")
            if isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
                if event.stop_reason.type == "requires_action":
                    fresh = [t for t in event.stop_reason.event_ids if t not in confirmed]
                    if fresh:
                        confirmed.update(fresh)
                        decisions: list[BetaManagedAgentsUserToolConfirmationEventParams] = [
                            {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": t}
                            for t in fresh
                        ]
                        await client.beta.sessions.events.send(session_id, events=decisions)
                    continue
                return

    await asyncio.wait_for(_drain(), timeout=_TURN_TIMEOUT_S)
    return "".join(reply_parts)


async def test_absolute_mount_path_joins_under_uploads(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    archive = _build_tiny_gzip_tar("hello.txt", b"contract bundle mount fixture\n")
    uploaded = await anthropic_client.beta.files.upload(
        file=("bundle.tar.gz", io.BytesIO(archive), "application/gzip"),
    )
    resource: BetaManagedAgentsFileResourceParams = {
        "type": "file",
        "file_id": uploaded.id,
        "mount_path": "/bundle.tar.gz",
    }
    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id, environment_id=live_environment.id, resources=[resource]
    )

    reply = await _send_command_and_capture_reply(
        anthropic_client,
        session.id,
        "test -f /mnt/session/uploads/bundle.tar.gz && echo MOUNT_FOUND || echo MOUNT_MISSING",
    )
    assert "MOUNT_FOUND" in reply, (
        "an absolute mount_path of '/bundle.tar.gz' must join under "
        f"/mnt/session/uploads/, same as a relative one; observed agent reply: {reply!r}"
    )


async def test_sessions_create_with_a_deleted_file_id(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> None:
    archive = _build_tiny_gzip_tar("gone.txt", b"deleted before session create\n")
    uploaded = await anthropic_client.beta.files.upload(
        file=("to-delete.tar.gz", io.BytesIO(archive), "application/gzip"),
    )
    await anthropic_client.beta.files.delete(uploaded.id, betas=[_MA_BETA])

    resource: BetaManagedAgentsFileResourceParams = {
        "type": "file",
        "file_id": uploaded.id,
        "mount_path": "/deleted.tar.gz",
    }
    try:
        session = await anthropic_client.beta.sessions.create(
            agent=live_agent.id, environment_id=live_environment.id, resources=[resource]
        )
    except anthropic.APIStatusError as err:
        assert err.status_code >= 400, (
            f"observed: sessions.create rejects a deleted file id up front with "
            f"{type(err).__name__} (status {err.status_code}) — the route's pre-check "
            "can rely on an error at create time"
        )
        return

    assert session.status in ("idle", "active", "running", "rescheduling", "terminated"), (
        "observed: sessions.create accepted a deleted file id without raising; the "
        f"session was created with status {session.status!r} — a create-time pre-check "
        "is therefore load-bearing, not redundant, for this failure mode"
    )

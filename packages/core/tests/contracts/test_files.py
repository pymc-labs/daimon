"""Contract tests pinning the live MA Files API facts the output-delivery
sweep is built on:

(a) mounted uploads list as non-downloadable while sandbox-written outputs
    list downloadable — the security gate keeping mounted credential files
    out of chat threads;
(b) a freshly written output is indexed within the sweep's poll window;
(c) deleting an entry and overwriting the same path produces a FRESH entry
    (new id, new content) — post-then-delete does not strand overwrites;
(d) an rm-and-recreate makes the file vanish from the listing entirely —
    why the agent guidance says overwrite-to-revise, never rm.

Runtime discipline: ONE module-scoped session, sequential turns inside it,
haiku only. Env-gated by DAIMON_TEST_ANTHROPIC_API_KEY; the default
`uv run pytest` deselects the contract marker so this never gates CI.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import uuid
from collections.abc import AsyncIterator

import anthropic
import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaCloudConfigParams,
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsDeltaEvent,
    BetaManagedAgentsSession,
    BetaManagedAgentsStartEvent,
    FileMetadata,
)
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
_INDEX_WINDOW_S = 15.0
_MOUNTED_NAME = "pinned.txt"

_ENV_CONFIG: BetaCloudConfigParams = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}


@pytest_asyncio.fixture(scope="module")
async def live_agent(anthropic_client: AsyncAnthropic) -> BetaManagedAgentsAgent:
    """Module-scoped fresh agent with the bash/read/write toolset."""
    name = f"contract-test-files-agent-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.agents.create(
        name=name,
        model={"id": "claude-haiku-4-5"},
        system=(
            "You are a test agent. Run the exact bash commands you are given, "
            "then reply exactly as instructed."
        ),
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [{"name": "bash"}, {"name": "read"}, {"name": "write"}],
            }
        ],
    )


@pytest_asyncio.fixture(scope="module")
async def live_environment(anthropic_client: AsyncAnthropic) -> BetaEnvironment:
    """Module-scoped fresh environment; the conftest workspace wipe cleans it."""
    name = f"contract-test-files-env-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.environments.create(name=name, config=_ENV_CONFIG)


@pytest_asyncio.fixture(scope="module")
async def live_session(
    anthropic_client: AsyncAnthropic,
    live_agent: BetaManagedAgentsAgent,
    live_environment: BetaEnvironment,
) -> AsyncIterator[BetaManagedAgentsSession]:
    """ONE session for the whole module, with a file mounted the way
    credential_env mounts the assembled .env — the subject of fact (a)."""
    uploaded = await anthropic_client.beta.files.upload(
        file=(_MOUNTED_NAME, io.BytesIO(b"mounted upload stand-in"), "text/plain"),
    )
    session = await anthropic_client.beta.sessions.create(
        agent=live_agent.id,
        environment_id=live_environment.id,
        resources=[{"type": "file", "file_id": uploaded.id, "mount_path": _MOUNTED_NAME}],
    )
    yield session
    # Best-effort file-entry cleanup; the conftest workspace wipe covers the
    # agent, environment and session themselves.
    page = await anthropic_client.beta.files.list(scope_id=session.id, betas=[_MA_BETA], limit=1000)
    for meta in page.data:
        with contextlib.suppress(anthropic.APIError):
            await anthropic_client.beta.files.delete(meta.id, betas=[_MA_BETA])


async def _run_command_turn(client: AsyncAnthropic, session_id: str, command: str) -> None:
    """Send one exact-command turn and drain the session stream to idle,
    auto-allowing requires_action tool confirmations."""
    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [
            {"type": "text", "text": f"Run exactly: {command}\nThen reply with exactly: DONE"}
        ],
    }
    await client.beta.sessions.events.send(session_id, events=[message])
    confirmed: set[str] = set()

    async def _drain() -> None:
        async for event in await client.beta.sessions.events.stream(session_id=session_id):
            if isinstance(event, BetaManagedAgentsStartEvent | BetaManagedAgentsDeltaEvent):
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
                            {
                                "type": "user.tool_confirmation",
                                "result": "allow",
                                "tool_use_id": t,
                            }
                            for t in fresh
                        ]
                        await client.beta.sessions.events.send(session_id, events=decisions)
                    continue
                return

    await asyncio.wait_for(_drain(), timeout=_TURN_TIMEOUT_S)


async def _list_entries(client: AsyncAnthropic, session_id: str) -> dict[str, FileMetadata]:
    page = await client.beta.files.list(scope_id=session_id, betas=[_MA_BETA], limit=1000)
    return {meta.filename: meta for meta in page.data}


async def _poll_for_filename(
    client: AsyncAnthropic, session_id: str, filename: str
) -> FileMetadata | None:
    """Poll the listing every 0.5s for up to the index window; None if absent."""
    deadline = asyncio.get_running_loop().time() + _INDEX_WINDOW_S
    while asyncio.get_running_loop().time() < deadline:
        entries = await _list_entries(client, session_id)
        if filename in entries:
            return entries[filename]
        await asyncio.sleep(0.5)
    return None


async def test_mounted_upload_is_not_downloadable_while_sandbox_output_is(
    anthropic_client: AsyncAnthropic,
    live_session: BetaManagedAgentsSession,
) -> None:
    """Fact (a): the downloadable split — the security gate the sweep rests on."""
    await _run_command_turn(
        anthropic_client,
        live_session.id,
        "mkdir -p /mnt/session/outputs && "
        "printf 'split-payload' > /mnt/session/outputs/split_check.txt",
    )
    written = await _poll_for_filename(anthropic_client, live_session.id, "split_check.txt")
    assert written is not None, (
        "a sandbox-written output must appear in the session listing within the poll window"
    )
    assert written.downloadable is True, (
        "sandbox-written outputs must list with downloadable=True — the sweep delivers only these"
    )

    entries = await _list_entries(anthropic_client, live_session.id)
    mounted = entries.get(_MOUNTED_NAME)
    assert mounted is not None, "the mounted upload must appear in the session listing"
    assert mounted.downloadable is not True, (
        "a mounted upload must NOT list as downloadable — without this split the sweep "
        "would post the mounted credential file into a chat thread"
    )


async def test_written_output_indexes_within_the_sweep_poll_window(
    anthropic_client: AsyncAnthropic,
    live_session: BetaManagedAgentsSession,
) -> None:
    """Fact (b): indexing lag from write fits inside the sweep's poll budget."""
    await _run_command_turn(
        anthropic_client,
        live_session.id,
        "mkdir -p /mnt/session/outputs && "
        "printf 'lag-payload' > /mnt/session/outputs/lag_check.txt",
    )
    entry = await _poll_for_filename(anthropic_client, live_session.id, "lag_check.txt")
    assert entry is not None, (
        "a freshly written output must be indexed within 15s of idle — the sweep's "
        "poll schedule (14s cumulative) depends on this bound"
    )


async def test_overwrite_after_delete_creates_fresh_entry_with_new_content(
    anthropic_client: AsyncAnthropic,
    live_session: BetaManagedAgentsSession,
) -> None:
    """Fact (c): post-then-delete does not strand overwrites — a rewrite after
    the entry was deleted re-lists under a NEW id with the NEW content."""
    await _run_command_turn(
        anthropic_client,
        live_session.id,
        "mkdir -p /mnt/session/outputs && printf 'v1' > /mnt/session/outputs/overwrite_check.txt",
    )
    first = await _poll_for_filename(anthropic_client, live_session.id, "overwrite_check.txt")
    assert first is not None, "the first write must be indexed before the delete step"

    await anthropic_client.beta.files.delete(first.id, betas=[_MA_BETA])
    await _run_command_turn(
        anthropic_client,
        live_session.id,
        "printf 'v2-fresh' > /mnt/session/outputs/overwrite_check.txt",
    )
    second = await _poll_for_filename(anthropic_client, live_session.id, "overwrite_check.txt")
    assert second is not None, "an overwrite after delete must produce a fresh listing entry"
    assert second.id != first.id, "the fresh entry must carry a NEW file id"

    response = await anthropic_client.beta.files.download(second.id, betas=[_MA_BETA])
    content = await response.read()
    assert content == b"v2-fresh", "the fresh entry must download to the NEW content"


async def test_rm_and_recreate_vanishes_from_the_listing(
    anthropic_client: AsyncAnthropic,
    live_session: BetaManagedAgentsSession,
) -> None:
    """Fact (d): rm-and-recreate removes the file from delivery entirely —
    the reason agent guidance says overwrite-to-revise, never rm."""
    await _run_command_turn(
        anthropic_client,
        live_session.id,
        "mkdir -p /mnt/session/outputs && printf 'v1' > /mnt/session/outputs/vanish_check.txt",
    )
    first = await _poll_for_filename(anthropic_client, live_session.id, "vanish_check.txt")
    assert first is not None, "the first write must be indexed before the rm step"

    await anthropic_client.beta.files.delete(first.id, betas=[_MA_BETA])
    await _run_command_turn(
        anthropic_client,
        live_session.id,
        "rm /mnt/session/outputs/vanish_check.txt && "
        "printf 'reborn' > /mnt/session/outputs/vanish_check.txt",
    )
    reappeared = await _poll_for_filename(anthropic_client, live_session.id, "vanish_check.txt")
    assert reappeared is None, (
        "an rm-and-recreate must NOT produce a new listing entry — the file vanishes "
        "from delivery, which is why guidance forbids rm-and-recreate"
    )

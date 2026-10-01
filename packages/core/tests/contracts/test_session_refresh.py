"""Contract tests pinning the live MA facts the mid-task refresh paths of
task-continuity depend on:

(a) a `.env` file resource can be replaced in place on a live session:
    `resources.add` at an occupied `mount_path` is rejected ("overlaps"),
    `resources.delete` actually unmounts the file, and the replacement is
    visible in the container on the very turn after `resources.add`;
(b) `sessions.update(agent={"tools": ...})` takes effect on the NEXT turn,
    never the one in flight;
(c) `sessions.update` is refused outright while a turn is running, with a
    message naming that reason;
(d) `system.message` must trail a `user.message` in the same request, is
    gated to sonnet-5-class models, and what it injects persists into a
    later turn that sends no `system.message` at all;
(e) an output tarball written under `/mnt/session/outputs/` round-trips
    through `files.download` -> `files.upload` -> mount into a brand-new
    session, byte-for-byte;
(f) `files.delete` on a session output leaves the sandbox's own copy on
    disk untouched.

Runtime discipline: haiku only except the system.message test (sonnet-5 is
required to accept it), one session per test, each archived at the end.
Turn waits are bounded to <=180s and the one `sleep` turn to <=60s. Env-gated
by DAIMON_TEST_ANTHROPIC_API_KEY; the default `uv run pytest` deselects the
contract marker so this never gates CI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import uuid
from collections.abc import Sequence

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic, BadRequestError
from anthropic.types.beta import (
    BetaCloudConfigParams,
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsAgentToolConfigParams,
    BetaManagedAgentsAgentToolset20260401Params,
    BetaManagedAgentsFileResourceParams,
    FileMetadata,
)
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsEventParams,
    BetaManagedAgentsFileResource,
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_system_message_event_params import (
    BetaManagedAgentsSystemMessageEventParams,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_tool_confirmation_event_params import (
    BetaManagedAgentsUserToolConfirmationEventParams,
)

pytestmark = pytest.mark.contract

_MA_BETA = "managed-agents-2026-04-01"
_TURN_TIMEOUT_S = 180.0
_SLEEP_TURN_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 1.5
_OUTPUT_INDEX_WINDOW_S = 20.0

_ENV_CONFIG: BetaCloudConfigParams = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}

_BASH_TOOL_CONFIG: BetaManagedAgentsAgentToolConfigParams = {"name": "bash"}
_BASH_TOOL: BetaManagedAgentsAgentToolset20260401Params = {
    "type": "agent_toolset_20260401",
    "configs": [_BASH_TOOL_CONFIG],
}


@pytest_asyncio.fixture(scope="module")
async def live_environment(anthropic_client: AsyncAnthropic) -> BetaEnvironment:
    """Module-scoped real environment, shared and cheap. Cleaned up by
    conftest's workspace wipe."""
    name = f"contract-test-refresh-env-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.environments.create(name=name, config=_ENV_CONFIG)


async def _make_bash_agent(client: AsyncAnthropic, suffix: str) -> BetaManagedAgentsAgent:
    name = f"contract-test-refresh-{suffix}-{uuid.uuid4().hex[:8]}"
    return await client.beta.agents.create(
        name=name,
        model={"id": "claude-haiku-4-5"},
        system=(
            "You are a test agent. Run the exact bash commands you are given, "
            "then reply exactly as instructed."
        ),
        tools=[_BASH_TOOL],
    )


async def _make_text_agent(
    client: AsyncAnthropic, suffix: str, *, model: str, system: str
) -> BetaManagedAgentsAgent:
    name = f"contract-test-refresh-{suffix}-{uuid.uuid4().hex[:8]}"
    return await client.beta.agents.create(name=name, model={"id": model}, system=system)


async def _wait_for_terminal_idle(
    client: AsyncAnthropic, session_id: str, since: dt.datetime, *, timeout_s: float
) -> list[BetaManagedAgentsSessionEvent]:
    """Poll (never stream) `events.list` for a terminal `session.status_idle`
    event at or after `since`, auto-allowing any tool confirmations as a
    safety net, and return every event observed in the window."""
    confirmed: set[str] = set()
    handled_idle_ids: set[str] = set()
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        events = [
            e
            async for e in client.beta.sessions.events.list(
                session_id=session_id, created_at_gte=since, order="asc", limit=200
            )
        ]
        for event in events:
            if not isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
                continue
            if event.id in handled_idle_ids:
                continue
            if event.stop_reason.type == "requires_action":
                handled_idle_ids.add(event.id)
                fresh = [t for t in event.stop_reason.event_ids if t not in confirmed]
                if fresh:
                    confirmed.update(fresh)
                    decisions: list[BetaManagedAgentsUserToolConfirmationEventParams] = [
                        {"type": "user.tool_confirmation", "result": "allow", "tool_use_id": t}
                        for t in fresh
                    ]
                    await client.beta.sessions.events.send(session_id, events=decisions)
                continue
            return events
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"session {session_id!r} did not reach a terminal idle within {timeout_s}s"
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _send_and_wait(
    client: AsyncAnthropic,
    session_id: str,
    events: Sequence[BetaManagedAgentsEventParams],
    *,
    timeout_s: float = _TURN_TIMEOUT_S,
) -> list[BetaManagedAgentsSessionEvent]:
    since = dt.datetime.now(dt.UTC)
    await client.beta.sessions.events.send(session_id, events=events)
    return await _wait_for_terminal_idle(client, session_id, since, timeout_s=timeout_s)


async def _run_turn(
    client: AsyncAnthropic, session_id: str, text: str, *, timeout_s: float = _TURN_TIMEOUT_S
) -> str:
    """Send one user.message turn and return the concatenated agent.message
    text once it reaches a terminal idle."""
    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": text}],
    }
    events = await _send_and_wait(client, session_id, [message], timeout_s=timeout_s)
    return "".join(
        block.text
        for event in events
        if isinstance(event, BetaManagedAgentsAgentMessageEvent)
        for block in event.content
    )


async def _poll_for_output_file(
    client: AsyncAnthropic,
    session_id: str,
    filename: str,
    *,
    timeout_s: float = _OUTPUT_INDEX_WINDOW_S,
) -> FileMetadata | None:
    """Poll `files.list(scope_id=...)` every second until `filename` is
    indexed, or None once `timeout_s` elapses."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        page = await client.beta.files.list(scope_id=session_id, betas=[_MA_BETA], limit=1000)
        for meta in page.data:
            if meta.filename == filename:
                return meta
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(1.0)


async def test_env_file_resource_can_be_replaced_in_place(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_bash_agent(anthropic_client, "env-replace")
    file_v1 = await anthropic_client.beta.files.upload(
        file=(".env-v1", io.BytesIO(b"PROBE_KEY=v1\n"), "text/plain")
    )
    env_resource: BetaManagedAgentsFileResourceParams = {
        "type": "file",
        "file_id": file_v1.id,
        "mount_path": ".env",
    }
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id, resources=[env_resource]
    )

    cat_v1 = await _run_turn(
        anthropic_client,
        session.id,
        "Run exactly: cat /mnt/session/uploads/.env\nThen reply with exactly the output.",
    )
    assert "v1" in cat_v1, (
        f"the mounted .env must be readable at /mnt/session/uploads/.env; got: {cat_v1!r}"
    )

    file_v2 = await anthropic_client.beta.files.upload(
        file=(".env-v2", io.BytesIO(b"PROBE_KEY=v2\n"), "text/plain")
    )
    with pytest.raises(BadRequestError) as exc_info:
        await anthropic_client.beta.sessions.resources.add(
            session.id, file_id=file_v2.id, type="file", mount_path=".env"
        )
    assert "overlaps" in (exc_info.value.message or "").lower(), (
        "resources.add at an occupied mount_path must reject mentioning 'overlaps'; "
        f"got: {exc_info.value.message!r}"
    )

    resources_page = [r async for r in anthropic_client.beta.sessions.resources.list(session.id)]
    env_resource_live = next(
        (
            r
            for r in resources_page
            if isinstance(r, BetaManagedAgentsFileResource) and r.mount_path.endswith(".env")
        ),
        None,
    )
    assert env_resource_live is not None, "the .env resource must be listed by resources.list"
    await anthropic_client.beta.sessions.resources.delete(
        env_resource_live.id, session_id=session.id
    )

    missing = await _run_turn(
        anthropic_client,
        session.id,
        "Run exactly: test -f /mnt/session/uploads/.env || echo MISSING\n"
        "Then reply with exactly the output.",
    )
    assert "MISSING" in missing, (
        f"resources.delete must actually unmount the .env file from the container; got: {missing!r}"
    )

    await anthropic_client.beta.sessions.resources.add(
        session.id, file_id=file_v2.id, type="file", mount_path=".env"
    )
    cat_v2 = await _run_turn(
        anthropic_client,
        session.id,
        "Run exactly: cat /mnt/session/uploads/.env\nThen reply with exactly the output.",
    )
    assert "v2" in cat_v2, (
        f"the replacement .env must be readable on the very turn after resources.add; got: {cat_v2!r}"
    )

    await anthropic_client.beta.sessions.archive(session.id)


async def test_tools_update_applies_on_the_next_turn(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_bash_agent(anthropic_client, "tools-update")
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id
    )

    await anthropic_client.beta.sessions.update(session.id, agent={"tools": []})

    since_cleared = dt.datetime.now(dt.UTC)
    await _run_turn(anthropic_client, session.id, "Run `echo hi` with bash and show the output.")
    events_without_tools = [
        e
        async for e in anthropic_client.beta.sessions.events.list(
            session_id=session.id,
            created_at_gte=since_cleared,
            types=["agent.tool_use"],
            order="asc",
        )
    ]
    assert not events_without_tools, (
        "a turn sent after sessions.update(agent={'tools': []}) must produce no "
        f"agent.tool_use event; observed {len(events_without_tools)}"
    )

    await anthropic_client.beta.sessions.update(session.id, agent={"tools": [_BASH_TOOL]})

    since_restored = dt.datetime.now(dt.UTC)
    await _run_turn(anthropic_client, session.id, "Run `echo hi` with bash and show the output.")
    events_with_tools = [
        e
        async for e in anthropic_client.beta.sessions.events.list(
            session_id=session.id,
            created_at_gte=since_restored,
            types=["agent.tool_use"],
            order="asc",
        )
    ]
    assert events_with_tools, (
        "a turn sent after sessions.update(agent={'tools': [BASH_TOOL]}) restored the "
        "toolset must produce at least one agent.tool_use event"
    )

    await anthropic_client.beta.sessions.archive(session.id)


async def test_update_is_refused_while_a_turn_is_running(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_bash_agent(anthropic_client, "update-running")
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id
    )

    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [
            {"type": "text", "text": "Run exactly: sleep 45\nThen reply with exactly: DONE"}
        ],
    }
    since = dt.datetime.now(dt.UTC)
    await anthropic_client.beta.sessions.events.send(session.id, events=[message])

    deadline = asyncio.get_running_loop().time() + 30.0
    running_status: str | None = None
    while asyncio.get_running_loop().time() < deadline:
        state = await anthropic_client.beta.sessions.retrieve(session.id)
        if state.status == "running":
            running_status = state.status
            break
        await asyncio.sleep(1.0)
    assert running_status == "running", (
        "the session never reached status 'running' while the sleep-45 turn was in flight "
        "-- cannot exercise the mid-turn update refusal without it"
    )

    with pytest.raises(BadRequestError) as exc_info:
        await anthropic_client.beta.sessions.update(session.id, agent={"tools": []})
    assert "while session is running" in (exc_info.value.message or "").lower(), (
        "sessions.update during a running turn must be rejected mentioning "
        f"'while session is running'; got: {exc_info.value.message!r}"
    )

    await _wait_for_terminal_idle(
        anthropic_client, session.id, since, timeout_s=_SLEEP_TURN_TIMEOUT_S
    )
    await anthropic_client.beta.sessions.archive(session.id)


async def test_system_message_must_trail_a_user_message_and_is_model_gated(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    text_system = "You are a test agent. Reply exactly as instructed, nothing else."
    sonnet_agent = await _make_text_agent(
        anthropic_client, "sysmsg-sonnet", model="claude-sonnet-5", system=text_system
    )
    sonnet_session = await anthropic_client.beta.sessions.create(
        agent=sonnet_agent.id, environment_id=live_environment.id
    )

    codeword = f"CODEWORD-DELTA-{uuid.uuid4().hex[:6]}"
    first_message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": "Reply with exactly: OK"}],
    }
    system_event: BetaManagedAgentsSystemMessageEventParams = {
        "type": "system.message",
        "content": [{"type": "text", "text": f"The briefing codeword is {codeword}."}],
    }
    accepted_events = await _send_and_wait(
        anthropic_client, sonnet_session.id, [first_message, system_event]
    )
    assert accepted_events, "the [user.message, system.message] turn must produce events"

    codeword_reply = await _run_turn(
        anthropic_client,
        sonnet_session.id,
        "What is the codeword from the briefing? Reply with only the codeword.",
    )
    assert codeword in codeword_reply, (
        "the fact injected by system.message on turn 1 must be answerable on turn 2 with no "
        f"system.message resent; got: {codeword_reply!r}"
    )

    reversed_user_message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": "Reply with exactly: OK"}],
    }
    with pytest.raises(BadRequestError) as exc_info:
        await anthropic_client.beta.sessions.events.send(
            sonnet_session.id, events=[system_event, reversed_user_message]
        )
    assert "must be the last event" in (exc_info.value.message or "").lower(), (
        "a [system.message, user.message] send (system first) must be rejected for "
        f"ordering; got: {exc_info.value.message!r}"
    )

    await anthropic_client.beta.sessions.archive(sonnet_session.id)

    haiku_agent = await _make_text_agent(
        anthropic_client, "sysmsg-haiku", model="claude-haiku-4-5", system=text_system
    )
    haiku_session = await anthropic_client.beta.sessions.create(
        agent=haiku_agent.id, environment_id=live_environment.id
    )
    haiku_message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": "Reply with exactly: OK"}],
    }
    haiku_system: BetaManagedAgentsSystemMessageEventParams = {
        "type": "system.message",
        "content": [{"type": "text", "text": "ctx"}],
    }
    with pytest.raises(BadRequestError) as exc_info:
        await anthropic_client.beta.sessions.events.send(
            haiku_session.id, events=[haiku_message, haiku_system]
        )
    assert "not supported on model" in (exc_info.value.message or "").lower(), (
        "system.message on a haiku session must be rejected as not supported on this model; "
        f"got: {exc_info.value.message!r}"
    )

    await anthropic_client.beta.sessions.archive(haiku_session.id)


async def test_output_tarball_round_trips_into_a_new_session(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    source_agent = await _make_bash_agent(anthropic_client, "tarball-source")
    source = await anthropic_client.beta.sessions.create(
        agent=source_agent.id, environment_id=live_environment.id
    )

    build_reply = await _run_turn(
        anthropic_client,
        source.id,
        "Run exactly this bash script:\n"
        "mkdir -p /tmp/handoff-src && cd /tmp/handoff-src\n"
        "printf 'contract handoff content' > known.txt\n"
        "mkdir -p /mnt/session/outputs\n"
        "tar czf /mnt/session/outputs/daimon-handoff-contract.tar.gz -C /tmp/handoff-src .\n"
        "sha256sum known.txt\n"
        "Then reply with exactly the full output of that script, nothing else.",
    )
    source_sha = next(
        (
            token
            for token in build_reply.split()
            if len(token) == 64 and all(c in "0123456789abcdef" for c in token)
        ),
        None,
    )
    assert source_sha is not None, (
        f"the build turn must print a sha256 for known.txt; got: {build_reply!r}"
    )

    written = await _poll_for_output_file(
        anthropic_client, source.id, "daimon-handoff-contract.tar.gz"
    )
    assert written is not None, (
        "the tarball written to /mnt/session/outputs/ must be indexed by files.list"
    )

    downloaded = await (
        await anthropic_client.beta.files.download(written.id, betas=[_MA_BETA])
    ).read()
    reuploaded = await anthropic_client.beta.files.upload(
        file=("daimon-handoff.tar.gz", io.BytesIO(downloaded), "application/gzip")
    )
    mount_resource: BetaManagedAgentsFileResourceParams = {
        "type": "file",
        "file_id": reuploaded.id,
        "mount_path": "/daimon-handoff.tar.gz",
    }
    dest_agent = await _make_bash_agent(anthropic_client, "tarball-dest")
    dest = await anthropic_client.beta.sessions.create(
        agent=dest_agent.id, environment_id=live_environment.id, resources=[mount_resource]
    )

    extract_reply = await _run_turn(
        anthropic_client,
        dest.id,
        "Run exactly this bash script:\n"
        "mkdir -p /tmp/handoff-dest\n"
        "tar xzf /mnt/session/uploads/daimon-handoff.tar.gz -C /tmp/handoff-dest\n"
        "sha256sum /tmp/handoff-dest/known.txt\n"
        "Then reply with exactly the full output of that script, nothing else.",
    )
    assert source_sha in extract_reply, (
        "the destination session must extract known.txt with the SAME sha256 as the source "
        f"session produced; source_sha={source_sha!r} extract_reply={extract_reply!r}"
    )

    await anthropic_client.beta.sessions.archive(source.id)
    await anthropic_client.beta.sessions.archive(dest.id)


async def test_files_delete_leaves_the_sandbox_copy(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_bash_agent(anthropic_client, "delete-survives")
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id
    )

    await _run_turn(
        anthropic_client,
        session.id,
        "Run exactly: mkdir -p /mnt/session/outputs && "
        "printf 'sandbox-copy-survives' > /mnt/session/outputs/delete_check.txt\n"
        "Then reply with exactly: DONE",
    )
    written = await _poll_for_output_file(anthropic_client, session.id, "delete_check.txt")
    assert written is not None, (
        "the output file must be indexed by files.list before it can be deleted"
    )

    await anthropic_client.beta.files.delete(written.id, betas=[_MA_BETA])

    survived = await _run_turn(
        anthropic_client,
        session.id,
        "Run exactly: cat /mnt/session/outputs/delete_check.txt\nThen reply with exactly the output.",
    )
    assert "sandbox-copy-survives" in survived, (
        "files.delete on a session output must leave the sandbox's own copy on disk -- the "
        f"container's copy must still be readable after delete; got: {survived!r}"
    )

    await anthropic_client.beta.sessions.archive(session.id)

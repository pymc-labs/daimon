"""Contract tests pinning the live MA session-snapshot facts the task-continuity
design depends on:

(a) `sessions.retrieve().agent` is a snapshot taken at create time — version,
    model, system and prompt are frozen even after `agents.update` changes the
    underlying agent, and a reused session keeps answering with the
    creation-time prompt;
(b) `sessions.update` accepts only `tools` and `mcp_servers`; `agent.model`,
    `agent.system`, `agent.skills`, `environment_id` and `vault_ids` are all
    rejected, each with the provider's own wording;
(c) `sessions.create(agent={"type": "agent", "version": N})` pins an exact
    agent version, and `agent_with_overrides` overrides — including an
    `mcp_servers`/`tools` pair that drops one server — are reflected in the
    session snapshot;
(d) the archived-session rejection text in `daimon.core.turn.run` still
    matches the live API, and `events.list` stays readable after
    `sessions.archive` but 404s after `sessions.delete`.

Runtime discipline: haiku only, no tools (every turn is a plain text reply),
one fresh agent+session per test, each archived at the end. Env-gated by
DAIMON_TEST_ANTHROPIC_API_KEY; the default `uv run pytest` deselects the
contract marker so this never gates CI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic, BadRequestError, NotFoundError
from anthropic.types.beta import (
    BetaCloudConfigParams,
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsAgentParams,
    BetaManagedAgentsAgentWithOverridesParams,
)
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)

pytestmark = pytest.mark.contract

_TURN_TIMEOUT_S = 180.0
_POLL_INTERVAL_S = 1.5

_EXACT_REPLY = "You are a test agent. Reply exactly as instructed, nothing else."

# daimon.core.turn.run._ARCHIVED_SESSION_MARKER, inlined rather than imported: it
# is a module-private constant (leading underscore, not in __all__). This test
# is the live canary that keeps it honest.
_ARCHIVED_SESSION_MARKER = "cannot send events to archived session"

_ENV_CONFIG: BetaCloudConfigParams = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}


@pytest_asyncio.fixture(scope="module")
async def live_environment(anthropic_client: AsyncAnthropic) -> BetaEnvironment:
    """Module-scoped real environment, shared and cheap. Cleaned up by
    conftest's workspace wipe."""
    name = f"contract-test-snapshot-env-{uuid.uuid4().hex[:8]}"
    return await anthropic_client.beta.environments.create(name=name, config=_ENV_CONFIG)


async def _make_agent(
    client: AsyncAnthropic, suffix: str, *, system: str
) -> BetaManagedAgentsAgent:
    """A fresh haiku agent with no tools -- every turn in this file is a plain
    text reply, so no tool-confirmation handling is needed anywhere below."""
    name = f"contract-test-snapshot-{suffix}-{uuid.uuid4().hex[:8]}"
    return await client.beta.agents.create(
        name=name, model={"id": "claude-haiku-4-5"}, system=system
    )


async def _run_turn(client: AsyncAnthropic, session_id: str, text: str) -> str:
    """Send one user.message turn and poll (never stream) `events.list` for the
    terminal `session.status_idle` event at or after the pre-send clock
    reading, returning the concatenated `agent.message` text."""
    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": text}],
    }
    since = dt.datetime.now(dt.UTC)
    await client.beta.sessions.events.send(session_id, events=[message])
    deadline = asyncio.get_running_loop().time() + _TURN_TIMEOUT_S
    while True:
        events = [
            e
            async for e in client.beta.sessions.events.list(
                session_id=session_id, created_at_gte=since, order="asc", limit=200
            )
        ]
        if any(isinstance(e, BetaManagedAgentsSessionStatusIdleEvent) for e in events):
            return "".join(
                block.text
                for e in events
                if isinstance(e, BetaManagedAgentsAgentMessageEvent)
                for block in e.content
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"turn on session {session_id!r} did not reach idle within {_TURN_TIMEOUT_S}s"
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def test_session_agent_is_frozen_at_creation(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_agent(
        anthropic_client, "frozen", system="contract snapshot test: frozen agent"
    )
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id
    )
    before = await anthropic_client.beta.sessions.retrieve(session.id)

    await anthropic_client.beta.agents.update(
        agent.id,
        version=agent.version,
        model={"id": "claude-sonnet-5"},
        system="mutated system prompt -- must not leak into the existing session",
    )

    after = await anthropic_client.beta.sessions.retrieve(session.id)
    assert after.agent.version == before.agent.version, (
        "sessions.retrieve().agent.version must stay pinned to the create-time agent "
        f"version after agents.update; before={before.agent.version} after={after.agent.version}"
    )
    assert after.agent.model.id == before.agent.model.id, (
        "sessions.retrieve().agent.model must stay pinned after agents.update bumped the "
        f"agent's model; before={before.agent.model.id!r} after={after.agent.model.id!r}"
    )
    assert after.agent.system == before.agent.system, (
        "sessions.retrieve().agent.system must stay pinned after agents.update bumped the "
        "agent's prompt"
    )

    await anthropic_client.beta.sessions.archive(session.id)


async def test_reused_session_answers_with_the_creation_time_prompt(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    tag = uuid.uuid4().hex[:8]
    agent = await _make_agent(
        anthropic_client,
        "codeword",
        system=f"{_EXACT_REPLY} If asked for the codeword, reply with exactly CODEWORD-ALPHA-{tag}.",
    )
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id
    )

    await anthropic_client.beta.agents.update(
        agent.id,
        version=agent.version,
        system=f"{_EXACT_REPLY} If asked for the codeword, reply with exactly CODEWORD-BRAVO-{tag}.",
    )

    reply = await _run_turn(
        anthropic_client, session.id, "What is the codeword? Reply with only the codeword."
    )
    assert f"CODEWORD-ALPHA-{tag}" in reply, (
        "a reused session must answer with the creation-time prompt's codeword (ALPHA) even "
        f"after agents.update changed the agent's prompt to BRAVO; got reply: {reply!r}"
    )
    assert f"CODEWORD-BRAVO-{tag}" not in reply, (
        f"the reply must not follow the updated agent's prompt (BRAVO); got: {reply!r}"
    )

    await anthropic_client.beta.sessions.archive(session.id)


async def test_update_rejects_model_system_skills_environment_and_vault_ids(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_agent(
        anthropic_client, "rejects", system="contract snapshot test: reject shapes"
    )
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id
    )

    rejection_cases: list[tuple[str, dict[str, object]]] = [
        ("model", {"agent": {"model": "claude-sonnet-5"}}),
        ("system", {"agent": {"system": "replaced by update"}}),
        ("skills", {"agent": {"skills": []}}),
    ]
    for label, extra_body in rejection_cases:
        with pytest.raises(BadRequestError) as exc_info:
            await anthropic_client.beta.sessions.update(session.id, extra_body=extra_body)
        message = (exc_info.value.message or "").lower()
        assert "only `tools` and `mcp_servers`" in message, (
            f"sessions.update(agent={{{label!r}: ...}}) must be rejected with the "
            f"'only `tools` and `mcp_servers` are updatable' wording; got: {message!r}"
        )

    with pytest.raises(BadRequestError) as exc_info:
        await anthropic_client.beta.sessions.update(
            session.id, extra_body={"environment_id": live_environment.id}
        )
    message = (exc_info.value.message or "").lower()
    assert "unknown field" in message, (
        f"sessions.update(environment_id=...) must be rejected as an unknown field; got: {message!r}"
    )

    with pytest.raises(BadRequestError) as exc_info:
        await anthropic_client.beta.sessions.update(session.id, vault_ids=["vlt_x"])
    message = (exc_info.value.message or "").lower()
    assert "not yet supported" in message, (
        f"sessions.update(vault_ids=...) must be rejected as not yet supported; got: {message!r}"
    )

    await anthropic_client.beta.sessions.archive(session.id)


async def test_version_pinning_runs_the_pinned_configuration(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    tag = uuid.uuid4().hex[:8]
    agent = await _make_agent(
        anthropic_client,
        "pinned",
        system=f"{_EXACT_REPLY} If asked for the codeword, reply with exactly CODEWORD-ALPHA-{tag}.",
    )
    await anthropic_client.beta.agents.update(
        agent.id,
        version=agent.version,
        system=f"{_EXACT_REPLY} If asked for the codeword, reply with exactly CODEWORD-BRAVO-{tag}.",
    )

    pinned_agent: BetaManagedAgentsAgentParams = {"type": "agent", "id": agent.id, "version": 1}
    session = await anthropic_client.beta.sessions.create(
        agent=pinned_agent, environment_id=live_environment.id
    )
    retrieved = await anthropic_client.beta.sessions.retrieve(session.id)
    assert retrieved.agent.version == 1, (
        f"a session created with agent={{'version': 1}} must retrieve with agent.version == 1; "
        f"got {retrieved.agent.version!r}"
    )

    reply = await _run_turn(
        anthropic_client, session.id, "What is the codeword? Reply with only the codeword."
    )
    assert f"CODEWORD-ALPHA-{tag}" in reply, (
        f"a session pinned to version 1 must run version 1's prompt (ALPHA); got: {reply!r}"
    )

    await anthropic_client.beta.sessions.archive(session.id)


async def test_agent_with_overrides_is_reflected_in_the_session_snapshot(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_agent(
        anthropic_client, "overrides", system="contract snapshot test: base prompt, never used"
    )
    override_system = "contract snapshot test: OVERRIDE prompt in effect"
    overridden_agent: BetaManagedAgentsAgentWithOverridesParams = {
        "type": "agent_with_overrides",
        "id": agent.id,
        "version": agent.version,
        "system": override_system,
    }
    session = await anthropic_client.beta.sessions.create(
        agent=overridden_agent, environment_id=live_environment.id
    )
    retrieved = await anthropic_client.beta.sessions.retrieve(session.id)
    assert retrieved.agent.system == override_system, (
        "sessions.retrieve().agent.system must equal the agent_with_overrides create-time "
        f"override; got {retrieved.agent.system!r}"
    )

    await anthropic_client.beta.sessions.archive(session.id)


async def test_agent_with_overrides_can_drop_one_mcp_server_and_its_toolset(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    """Per-caller MCP visibility rides on this: a session created with
    `mcp_servers`/`tools` overrides must run the shorter arrays, and MA must
    accept a pair that drops a server together with its toolset."""
    name = f"contract-test-snapshot-mcp-{uuid.uuid4().hex[:8]}"
    agent = await anthropic_client.beta.agents.create(
        name=name,
        model={"id": "claude-haiku-4-5"},
        system="contract snapshot test: mcp server overrides",
        mcp_servers=[
            {"type": "url", "name": "personal", "url": "https://mcp.example.com/personal"},
            {"type": "url", "name": "shared", "url": "https://mcp.example.com/shared"},
        ],
        tools=[
            {"type": "mcp_toolset", "mcp_server_name": "personal"},
            {"type": "mcp_toolset", "mcp_server_name": "shared"},
        ],
    )
    overridden_agent: BetaManagedAgentsAgentWithOverridesParams = {
        "type": "agent_with_overrides",
        "id": agent.id,
        "mcp_servers": [{"type": "url", "name": "shared", "url": "https://mcp.example.com/shared"}],
        "tools": [{"type": "mcp_toolset", "mcp_server_name": "shared"}],
    }
    session = await anthropic_client.beta.sessions.create(
        agent=overridden_agent, environment_id=live_environment.id
    )
    retrieved = await anthropic_client.beta.sessions.retrieve(session.id)

    assert [server.name for server in retrieved.agent.mcp_servers] == ["shared"], (
        "sessions.retrieve().agent.mcp_servers must equal the create-time override; got "
        f"{[server.name for server in retrieved.agent.mcp_servers]!r}"
    )
    assert not any(
        getattr(tool, "mcp_server_name", None) == "personal" for tool in retrieved.agent.tools
    ), "the dropped server's toolset must not survive the override"
    assert retrieved.agent.id == agent.id, (
        "an overridden session must still report the agent it names: `ma_agent_id` is an "
        f"identity-fingerprint field, so a different id replaces the session every turn; got "
        f"{retrieved.agent.id!r}"
    )

    await anthropic_client.beta.sessions.archive(session.id)


async def test_archived_session_rejects_events_with_the_marker_daimon_matches(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_agent(
        anthropic_client, "archived", system="contract snapshot test: archived marker"
    )
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id
    )
    await anthropic_client.beta.sessions.archive(session.id)

    message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": "ping"}],
    }
    with pytest.raises(BadRequestError) as exc_info:
        await anthropic_client.beta.sessions.events.send(session.id, events=[message])

    observed = (exc_info.value.message or "").lower()
    assert _ARCHIVED_SESSION_MARKER in observed, (
        "the archived-session rejection message no longer matches "
        f"daimon.core.turn.run._ARCHIVED_SESSION_MARKER ({_ARCHIVED_SESSION_MARKER!r}); "
        f"got: {observed!r}"
    )


async def test_events_list_is_readable_after_archive_and_gone_after_delete(
    anthropic_client: AsyncAnthropic, live_environment: BetaEnvironment
) -> None:
    agent = await _make_agent(
        anthropic_client, "readable", system=f"{_EXACT_REPLY} Always reply with exactly: PONG."
    )
    session = await anthropic_client.beta.sessions.create(
        agent=agent.id, environment_id=live_environment.id
    )
    await _run_turn(anthropic_client, session.id, "Reply with exactly: PONG")

    await anthropic_client.beta.sessions.archive(session.id)
    events_after_archive = [
        e
        async for e in anthropic_client.beta.sessions.events.list(
            session_id=session.id, order="asc"
        )
    ]
    assert events_after_archive, (
        "events.list must still return the session's event log after sessions.archive -- "
        "transcript-carry after archive is a supported recovery rung"
    )

    # A delete on an already-archived session: archive only closes the session to
    # new events, delete removes the resource itself, so the two are expected to
    # compose. If the live API refuses this, this assertion is the record of it.
    await anthropic_client.beta.sessions.delete(session.id)
    with pytest.raises(NotFoundError):
        [e async for e in anthropic_client.beta.sessions.events.list(session_id=session.id)]

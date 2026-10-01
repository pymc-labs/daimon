"""The stateful sessions fake must satisfy real SDK parsing end-to-end.

Every request in this file goes through a real `AsyncAnthropic` client backed
by `httpx.MockTransport` (`build_fake_anthropic`) -- the SDK's own parameter
validation and response parsing run in full, exactly like `test_ma.py` and
`test_ma_memory_fake.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import anthropic
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from daimon.testing.ma import (
    FakeMAState,
    FakeMemoryStoreState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from daimon.testing.ma_sessions import (
    FakeSessionsState,
    make_fake_sessions_handler,
    session_turn_sse,
)


def _client(
    state: FakeSessionsState, memory_state: FakeMemoryStoreState | None = None
) -> AsyncAnthropic:
    """Compose the sessions fake with the memory-store and agent fakes.

    Ordering matters: `make_fake_ma_handler` never raises `NotHandled` (its
    last branch is its own 404), so it MUST be last or it would swallow every
    request meant for the sessions/memory-store handlers.
    """
    return build_fake_anthropic(
        combine_handlers(
            make_fake_sessions_handler(state),
            make_fake_memory_store_handler(memory_state),
            make_fake_ma_handler(state.ma),
        )
    )


async def _make_agent(client: AsyncAnthropic, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {"name": "test-agent", "model": "claude-sonnet-4-6"}
    kwargs.update(overrides)
    agent = await client.beta.agents.create(**kwargs)
    return agent.id


def _rid(resource: object) -> str:
    """`.id` on a session-resource / stream-event union member: not every
    variant declares one (memory_store resources, start/delta preview
    events), so narrow with getattr rather than a static `.id` access."""
    resource_id = getattr(resource, "id", None)
    assert isinstance(resource_id, str), f"expected an object with a string id, got {resource!r}"
    return resource_id


def _combined_handler_no_ma(state: FakeSessionsState):
    """combine_handlers WITHOUT the agent fake, to prove this handler alone
    raises NotHandled (rather than swallowing) for unmatched paths."""
    return combine_handlers(make_fake_sessions_handler(state))


# ---------------------------------------------------------------------------
# create / retrieve round trip, with resources
# ---------------------------------------------------------------------------


async def test_create_and_retrieve_session_with_file_resource_round_trips() -> None:
    ma_state = FakeMAState()
    state = FakeSessionsState(ma=ma_state)
    client = _client(state)

    agent_id = await _make_agent(client)
    uploaded = await client.beta.files.upload(file=("notes.txt", b"hello world", "text/plain"))

    session = await client.beta.sessions.create(
        agent=agent_id,
        environment_id="env_test",
        resources=[{"type": "file", "file_id": uploaded.id, "mount_path": "notes.txt"}],
    )
    assert session.agent.id == agent_id, "session.agent should snapshot the created agent"
    assert session.status == "idle"
    assert len(session.resources) == 1
    resource = session.resources[0]
    assert resource.type == "file"
    assert resource.mount_path == "/mnt/session/uploads/notes.txt", (
        "a relative mount_path joins under the fixed uploads root"
    )
    assert state.sandbox[session.id][resource.mount_path] == b"hello world"

    got = await client.beta.sessions.retrieve(session.id)
    assert got.id == session.id
    assert got.resources[0].mount_path == resource.mount_path


async def test_create_session_rejects_unknown_file_id() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)

    with pytest.raises(Exception, match="No such file"):
        await client.beta.sessions.create(
            agent=agent_id,
            environment_id="env_test",
            resources=[{"type": "file", "file_id": "file_doesnotexist", "mount_path": "x"}],
        )


async def test_create_session_with_memory_store_and_github_resources() -> None:
    ma_state = FakeMAState()
    memory_state = FakeMemoryStoreState()
    state = FakeSessionsState(ma=ma_state)
    client = _client(state, memory_state)
    agent_id = await _make_agent(client)

    store = await client.beta.memory_stores.create(name="agent memory")

    session = await client.beta.sessions.create(
        agent=agent_id,
        environment_id="env_test",
        resources=[
            {"type": "memory_store", "memory_store_id": store.id, "access": "read_write"},
            {
                "type": "github_repository",
                "url": "https://github.com/pymc-labs/daimon",
                "authorization_token": "ghp_test",
            },
        ],
    )
    types = {r.type for r in session.resources}
    assert types == {"memory_store", "github_repository"}
    github = next(r for r in session.resources if r.type == "github_repository")
    assert github.mount_path == "/workspace/daimon"


async def test_mount_path_join_rule_bare_and_leading_slash_land_the_same() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    f1 = await client.beta.files.upload(file=("a.env", b"A=1", "text/plain"))
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    added = await client.beta.sessions.resources.add(
        session.id, file_id=f1.id, type="file", mount_path="/a.env"
    )
    assert added.mount_path == "/mnt/session/uploads/a.env"


# ---------------------------------------------------------------------------
# snapshot freeze
# ---------------------------------------------------------------------------


async def test_session_agent_snapshot_is_frozen_after_agents_update() -> None:
    ma_state = FakeMAState()
    state = FakeSessionsState(ma=ma_state)
    client = _client(state)
    agent_id = await _make_agent(client, model="claude-sonnet-4-6")

    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    assert session.agent.version == 1
    assert session.agent.model.id == "claude-sonnet-4-6"

    await client.beta.agents.update(agent_id, version=1, model="claude-opus-4-6")

    got = await client.beta.sessions.retrieve(session.id)
    assert got.agent.version == 1, "a later agents.update must not move the session's snapshot"
    assert got.agent.model.id == "claude-sonnet-4-6", (
        "GET must return the stored session, never re-read state.ma.agents"
    )


async def test_agent_with_overrides_and_pinned_version() -> None:
    ma_state = FakeMAState()
    state = FakeSessionsState(ma=ma_state)
    client = _client(state)
    agent_id = await _make_agent(client, model="claude-sonnet-4-6")
    await client.beta.agents.update(agent_id, version=1, model="claude-opus-4-6")  # -> version 2

    session = await client.beta.sessions.create(
        agent={"type": "agent", "id": agent_id, "version": 1},
        environment_id="env_test",
    )
    assert session.agent.version == 1
    assert session.agent.model.id == "claude-sonnet-4-6", "version=1 pins the pre-update config"

    overridden = await client.beta.sessions.create(
        agent={
            "type": "agent_with_overrides",
            "id": agent_id,
            "model": "claude-haiku-4-5",
        },
        environment_id="env_test",
    )
    assert overridden.agent.model.id == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# sessions.update whitelist / metadata / vault_ids / pending-applies-on-send
# ---------------------------------------------------------------------------


async def test_update_rejects_vault_ids() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    with pytest.raises(Exception, match="Not yet supported"):
        await client.beta.sessions.update(session.id, vault_ids=["vlt_123"])


async def test_update_rejects_unknown_agent_field() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    # The SDK's own SessionAgentUpdateParam only declares tools/mcp_servers;
    # this bypasses that typing deliberately to prove the fake rejects a
    # field the real API also rejects.
    bad_agent = cast(Any, {"model": "claude-opus-4-6"})
    with pytest.raises(Exception, match="only 'tools' and 'mcp_servers'"):
        await client.beta.sessions.update(session.id, agent=bad_agent)


async def test_update_metadata_enforces_pair_and_length_caps() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    too_many: dict[str, str | None] = {f"k{i}": "v" for i in range(17)}
    with pytest.raises(Exception, match="16 pairs"):
        await client.beta.sessions.update(session.id, metadata=too_many)

    with pytest.raises(Exception, match="64 chars"):
        await client.beta.sessions.update(session.id, metadata={"k" * 65: "v"})

    with pytest.raises(Exception, match="512 chars"):
        await client.beta.sessions.update(session.id, metadata={"k": "v" * 513})


async def test_pending_update_applies_only_on_next_events_send() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    assert session.agent.mcp_servers == []

    updated_response = await client.beta.sessions.update(
        session.id, agent={"mcp_servers": []}, title="new title"
    )
    assert updated_response.title is None, "update response mirrors the STORED session, unchanged"

    still_old = await client.beta.sessions.retrieve(session.id)
    assert still_old.title is None, "GET must not reflect a pending, not-yet-applied update"

    await client.beta.sessions.events.send(
        session.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]
    )

    after = await client.beta.sessions.retrieve(session.id)
    assert after.title == "new title", "the pending patch applies once the next turn is sent"


async def test_update_tools_full_replacement_preserves_omitted_mcp_servers() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    custom_tool = {
        "type": "custom",
        "name": "lookup",
        "description": "look things up",
        "input_schema": {"type": "object"},
    }
    agent_id = await _make_agent(client, tools=[custom_tool])
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    assert len(session.agent.tools) == 1

    # Replace tools with an empty list; omit mcp_servers entirely.
    await client.beta.sessions.update(session.id, agent={"tools": []})
    await client.beta.sessions.events.send(
        session.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]
    )
    after = await client.beta.sessions.retrieve(session.id)
    assert after.agent.tools == [], "provided array fully replaces"
    assert after.agent.mcp_servers == [], "omitted field is preserved (was already empty)"


# ---------------------------------------------------------------------------
# archive / delete
# ---------------------------------------------------------------------------


async def test_archive_then_send_events_400_with_marker_text() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    archived = await client.beta.sessions.archive(session.id)
    assert archived.archived_at is not None

    with pytest.raises(Exception) as exc_info:
        await client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}],
        )
    # Production's dead-session detector matches this marker lowercased.
    assert "cannot send events to archived session" in str(exc_info.value).lower()


async def test_delete_then_retrieve_and_send_events_404() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    deleted = await client.beta.sessions.delete(session.id)
    assert deleted.id == session.id

    with pytest.raises(anthropic.NotFoundError):
        await client.beta.sessions.retrieve(session.id)
    with pytest.raises(anthropic.APIStatusError):
        await client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}],
        )


# ---------------------------------------------------------------------------
# sessions.list
# ---------------------------------------------------------------------------


async def test_sessions_list_filters_by_agent_and_status_and_archived() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_a = await _make_agent(client, name="agent-a")
    agent_b = await _make_agent(client, name="agent-b")

    session_a = await client.beta.sessions.create(agent=agent_a, environment_id="env_test")
    await client.beta.sessions.create(agent=agent_b, environment_id="env_test")
    await client.beta.sessions.archive(session_a.id)

    only_b = [s async for s in client.beta.sessions.list(agent_id=agent_b)]
    assert {s.agent.id for s in only_b} == {agent_b}

    excludes_archived = [s async for s in client.beta.sessions.list(agent_id=agent_a)]
    assert excludes_archived == []

    includes_archived = [
        s async for s in client.beta.sessions.list(agent_id=agent_a, include_archived=True)
    ]
    assert {s.id for s in includes_archived} == {session_a.id}


async def test_sessions_list_rejects_unknown_query_param() -> None:
    """`metadata` is not a real sessions.list filter -- exercised via a raw
    request since the typed SDK method has no such kwarg to bypass through."""
    state = FakeSessionsState(ma=FakeMAState())
    handler = make_fake_sessions_handler(state)
    request = httpx.Request(
        "GET", "https://api.anthropic.com/v1/sessions?beta=true&metadata=daimon_tenant%3Ax"
    )
    response = handler(request)
    assert response.status_code == 400
    assert "Unknown query parameter" in response.text


# ---------------------------------------------------------------------------
# session resources: add / list / delete / update
# ---------------------------------------------------------------------------


async def test_resources_add_is_file_only() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    # resources.add's `type` kwarg is `Literal["file"]` at the SDK level, so a
    # non-file type can only be reached by bypassing that typing.
    with pytest.raises(Exception, match="Only file resources"):
        await client.beta.sessions.resources.add(
            session.id,
            file_id="file_x",
            type=cast(Any, "github_repository"),
        )


async def test_resources_add_duplicate_mount_path_rejected_by_default() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    f1 = await client.beta.files.upload(file=("a.txt", b"1", "text/plain"))
    f2 = await client.beta.files.upload(file=("b.txt", b"2", "text/plain"))

    await client.beta.sessions.resources.add(
        session.id, file_id=f1.id, type="file", mount_path="shared"
    )
    with pytest.raises(Exception, match="mount_path already in use"):
        await client.beta.sessions.resources.add(
            session.id, file_id=f2.id, type="file", mount_path="shared"
        )


async def test_resources_list_add_delete_round_trip() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    f1 = await client.beta.files.upload(file=("a.txt", b"1", "text/plain"))

    added = await client.beta.sessions.resources.add(session.id, file_id=f1.id, type="file")
    listed = [r async for r in client.beta.sessions.resources.list(session.id)]
    assert [_rid(r) for r in listed] == [added.id]

    deleted = await client.beta.sessions.resources.delete(added.id, session_id=session.id)
    assert deleted.id == added.id
    listed_after = [r async for r in client.beta.sessions.resources.list(session.id)]
    assert listed_after == []

    got_session = await client.beta.sessions.retrieve(session.id)
    assert got_session.resources == [], "session.resources stays in sync with resources.delete"


async def test_resources_update_authorization_token_is_github_only() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    f1 = await client.beta.files.upload(file=("a.txt", b"1", "text/plain"))
    file_resource = await client.beta.sessions.resources.add(session.id, file_id=f1.id, type="file")

    with pytest.raises(Exception, match="github_repository"):
        await client.beta.sessions.resources.update(
            file_resource.id, session_id=session.id, authorization_token="ghp_new"
        )

    github_resource = await client.beta.sessions.create(
        agent=agent_id,
        environment_id="env_test",
        resources=[
            {
                "type": "github_repository",
                "url": "https://github.com/pymc-labs/daimon",
                "authorization_token": "ghp_old",
            }
        ],
    )
    rid = _rid(github_resource.resources[0])
    updated = await client.beta.sessions.resources.update(
        rid, session_id=github_resource.id, authorization_token="ghp_new"
    )
    assert _rid(updated) == rid


# ---------------------------------------------------------------------------
# events.send: system.message ordering + model gate
# ---------------------------------------------------------------------------


async def test_system_message_must_be_final_and_preceded_by_user_message() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client, model="claude-sonnet-5")
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    with pytest.raises(Exception, match="final event"):
        await client.beta.sessions.events.send(
            session.id,
            events=[
                {"type": "system.message", "content": [{"type": "text", "text": "sys"}]},
                {"type": "user.message", "content": [{"type": "text", "text": "hi"}]},
            ],
        )

    with pytest.raises(Exception, match="immediately follow"):
        await client.beta.sessions.events.send(
            session.id,
            events=[{"type": "system.message", "content": [{"type": "text", "text": "sys"}]}],
        )

    with pytest.raises(Exception, match="At most one system.message"):
        await client.beta.sessions.events.send(
            session.id,
            events=[
                {"type": "user.message", "content": [{"type": "text", "text": "hi"}]},
                {"type": "system.message", "content": [{"type": "text", "text": "a"}]},
                {"type": "system.message", "content": [{"type": "text", "text": "b"}]},
            ],
        )


async def test_system_message_accepted_on_gated_model() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client, model="claude-sonnet-5")
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    result = await client.beta.sessions.events.send(
        session.id,
        events=[
            {"type": "user.message", "content": [{"type": "text", "text": "hi"}]},
            {"type": "system.message", "content": [{"type": "text", "text": "be terse"}]},
        ],
    )
    assert result.data is not None
    assert [e.type for e in result.data] == ["user.message", "system.message"]
    for event in result.data:
        assert event.processed_at is None, "every echoed event carries processed_at=None"


async def test_system_message_rejected_on_ungated_model() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client, model="claude-haiku-4-5")
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    with pytest.raises(Exception, match="not supported on model"):
        await client.beta.sessions.events.send(
            session.id,
            events=[
                {"type": "user.message", "content": [{"type": "text", "text": "hi"}]},
                {"type": "system.message", "content": [{"type": "text", "text": "be terse"}]},
            ],
        )


async def test_send_events_records_sent_batches_in_order() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    await client.beta.sessions.events.send(
        session.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "one"}]}]
    )
    await client.beta.sessions.events.send(
        session.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "two"}]}]
    )
    assert [sid for sid, _ in state.sent_batches] == [session.id, session.id]
    assert len(state.sent_batches) == 2


# ---------------------------------------------------------------------------
# events.list: filters + pagination
# ---------------------------------------------------------------------------


async def test_events_list_filters_by_type_and_paginates() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    for i in range(3):
        await client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": str(i)}]}],
        )

    all_events = [e async for e in client.beta.sessions.events.list(session.id)]
    assert len(all_events) == 3
    assert all(e.type == "user.message" for e in all_events)

    typed = [e async for e in client.beta.sessions.events.list(session.id, types=["user.message"])]
    assert len(typed) == 3
    none_matching = [
        e async for e in client.beta.sessions.events.list(session.id, types=["agent.message"])
    ]
    assert none_matching == []

    first_page = await client.beta.sessions.events.list(session.id, limit=1)
    assert len(first_page.data) == 1, "limit should cap a single page's size"
    assert first_page.next_page is not None, "more events remain beyond the first page"


async def test_events_listable_after_archive_by_default() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    await client.beta.sessions.events.send(
        session.id, events=[{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}]
    )
    await client.beta.sessions.archive(session.id)

    events = [e async for e in client.beta.sessions.events.list(session.id)]
    assert len(events) == 1

    state.events_listable_after_archive = False
    with pytest.raises(anthropic.APIStatusError):
        [e async for e in client.beta.sessions.events.list(session.id)]


# ---------------------------------------------------------------------------
# events.stream
# ---------------------------------------------------------------------------


async def test_stream_serves_queued_script_then_falls_back_to_idle() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    idle = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_scripted",
        processed_at=datetime.now(UTC),
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        type="session.status_idle",
    )
    state.stream_scripts[session.id] = [session_turn_sse(idle)]

    stream = await client.beta.sessions.events.stream(session_id=session.id)
    seen = [event async for event in stream]
    assert len(seen) == 1
    assert _rid(seen[0]) == "evt_scripted"

    # Script exhausted: falls back to the minimal idle-only stream.
    stream_2 = await client.beta.sessions.events.stream(session_id=session.id)
    seen2 = [event async for event in stream_2]
    assert len(seen2) == 1
    assert seen2[0].type == "session.status_idle"


# ---------------------------------------------------------------------------
# Files API
# ---------------------------------------------------------------------------


async def test_files_upload_list_by_scope_download_delete_round_trip() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")

    uploaded = await client.beta.files.upload(file=("input.csv", b"a,b\n1,2", "text/csv"))
    assert uploaded.downloadable is False, "an uploaded (not yet mounted) file is not downloadable"

    await client.beta.sessions.resources.add(session.id, file_id=uploaded.id, type="file")
    output_meta = state.write_output(session.id, "report.pdf", b"%PDF-fake")
    assert output_meta.downloadable is True

    scoped = [f async for f in client.beta.files.list(scope_id=session.id)]
    scoped_ids = {f.id for f in scoped}
    assert uploaded.id in scoped_ids
    assert output_meta.id in scoped_ids

    content = await client.beta.files.download(output_meta.id)
    body = await content.read()
    assert body == b"%PDF-fake"

    deleted = await client.beta.files.delete(output_meta.id)
    assert deleted.id == output_meta.id
    with pytest.raises(anthropic.NotFoundError):
        await client.beta.files.download(output_meta.id)


async def test_files_delete_unmounts_when_flag_set() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    state.delete_unmounts = True
    client = _client(state)
    agent_id = await _make_agent(client)
    session = await client.beta.sessions.create(agent=agent_id, environment_id="env_test")
    uploaded = await client.beta.files.upload(file=("a.env", b"A=1", "text/plain"))
    resource = await client.beta.sessions.resources.add(
        session.id, file_id=uploaded.id, type="file"
    )
    assert resource.mount_path in state.sandbox[session.id]

    await client.beta.files.delete(uploaded.id)
    assert resource.mount_path not in state.sandbox[session.id], (
        "delete_unmounts=True removes the sandbox copy when its source file is deleted"
    )


# ---------------------------------------------------------------------------
# combine_handlers composition / NotHandled contract
# ---------------------------------------------------------------------------


async def test_sessions_handler_alone_raises_not_handled_for_agents_path() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    handler = _combined_handler_no_ma(state)
    request = httpx.Request("GET", "https://api.anthropic.com/v1/agents")
    with pytest.raises(AssertionError, match="No handler matched"):
        handler(request)


def test_sessions_handler_raises_not_handled_directly_for_unmatched_path() -> None:
    state = FakeSessionsState(ma=FakeMAState())
    handler = make_fake_sessions_handler(state)
    request = httpx.Request("GET", "https://api.anthropic.com/v1/agents")
    with pytest.raises(NotHandled):
        handler(request)


async def test_agent_fake_must_be_last_in_combine_handlers() -> None:
    """Documents the required ordering: make_fake_ma_handler ends in its own
    404 catch-all rather than raising NotHandled, so putting it before the
    sessions handler would swallow every /v1/sessions request."""
    ma_state = FakeMAState()
    state = FakeSessionsState(ma=ma_state)
    misordered = build_fake_anthropic(
        combine_handlers(make_fake_ma_handler(ma_state), make_fake_sessions_handler(state))
    )
    agent = await misordered.beta.agents.create(name="a", model="claude-sonnet-4-6")
    with pytest.raises(anthropic.APIStatusError):
        # The agent fake's catch-all 404s this instead of the sessions
        # handler ever seeing it.
        await misordered.beta.sessions.create(agent=agent.id, environment_id="env_test")

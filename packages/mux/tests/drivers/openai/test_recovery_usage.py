from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest
from mux.contracts.events import RequiresActionPayload
from mux.contracts.ids import PageRequest
from mux.contracts.usage import UsageObservation
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import objects
from mux.drivers.openai.transport import Method, Object, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError

from .conftest import (
    REF,
    SCOPE,
    FakeTransport,
    Source,
    event,
    message,
    native_session,
    page,
    required_action_event,
    turn,
)


@pytest.mark.asyncio
async def test_recovery_connects_first_final_item_dedup_and_gap(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    transport.hold = True
    transport.stream_values = [
        event("turn.item.done", "live-item", item=message(), turn_id="root"),
        event(
            "turn.output_text.delta", "late-delta", item_id="item", content_index=0, delta="stale"
        ),
    ]
    projection = await driver.events.reconcile(SCOPE, REF)
    assert transport.calls[0][1].endswith("/events")
    assert projection.gaps == ("missed_native_events",)
    history = await driver.events.list(SCOPE, REF, page=PageRequest())
    assert sum(e.type == "agent.message" for e in history.data) == 1
    assert sum(e.type == "session.turn_ended" for e in history.data) == 1
    assert not any(e.authority == "preview" for e in history.data)
    assert transport.sources[0].closed
    paged = await driver.events.list(SCOPE, REF, page=PageRequest(limit=1))
    assert paged.has_more and paged.next_cursor == history.data[0].id
    after = await driver.events.list(
        SCOPE, REF, page=PageRequest(cursor=paged.next_cursor, limit=100)
    )
    assert after.data == history.data[1:] and after.next_cursor is None


@pytest.mark.asyncio
async def test_recovery_eof_cannot_publish_success(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    with pytest.raises(ProviderError, match="transient_network"):
        await driver.events.reconcile(SCOPE, REF)
    assert transport.sources[0].closed


@pytest.mark.asyncio
async def test_recovery_snapshot_failure_keeps_previous_journal(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    before = await driver.events.list(SCOPE, REF, page=PageRequest())
    transport.hold = True
    transport.responses["GET", "/agents/sessions/s/turns"] = ProviderError(
        "transient_network", retryable=True
    )
    with pytest.raises(ProviderError):
        await driver.events.reconcile(SCOPE, REF)
    transport.responses["GET", "/agents/sessions/s/turns"] = page(turn())
    assert (await driver.events.list(SCOPE, REF, page=PageRequest())) == before
    assert transport.sources[0].closed


@pytest.mark.asyncio
async def test_recovery_child_end_and_idle_cannot_release_active_root(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    transport.hold = True
    transport.responses["GET", "/agents/sessions/s"] = native_session("in_progress")
    transport.responses["GET", "/agents/sessions/s/turns"] = page(
        turn("in_progress"), turn(id_="child", child="sub")
    )
    transport.stream_values = [
        event("turn.completed", "child", turn=turn(id_="child", child="sub")),
        event("idle", "idle", session=native_session()),
    ]
    result = await driver.events.reconcile(SCOPE, REF)
    assert result.state == "running" and result.active_root_turn == "root"


@pytest.mark.asyncio
async def test_usage_null_revisions_signed_corrections_replay_and_restart(
    transport: FakeTransport,
) -> None:
    journal, revisions = MemoryRecoveryJournal(), MemoryUsageRevisions()

    def make() -> OpenAIDriver:
        return OpenAIDriver(
            transport,
            account_scope_id="project",
            journal=journal,
            usage_revisions=revisions,
            authorization=lambda s, k, i: s == SCOPE,
        )

    driver = make()
    values: list[UsageObservation] = []
    for expected_revision, count in enumerate((None, 100, 120, 110), 1):
        native = turn()
        native["usage"] = (
            None
            if count is None
            else {
                "input_tokens": count,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens": 10,
                "output_tokens_details": {"reasoning_tokens": 3},
            }
        )
        transport.responses["GET", "/agents/sessions/s/turns"] = page(native)
        observation = (await driver.usage.reconcile(SCOPE, REF))[0]
        values.append(observation)
        assert observation.revision == expected_revision and observation.input_tokens == count
        assert observation.input_cache_write_tokens is None and observation.model is None
        replay = (await driver.usage.reconcile(SCOPE, REF))[0]
        assert replay.revision == observation.revision
        driver = make()
    assert len({o.id for o in values}) == 1
    assert values[0].completeness == "unknown"
    assert values[1].input_cached_tokens == 20 and values[1].output_reasoning_tokens == 3
    from mux.state.usage_ledger import apply_observation

    prior = None
    deltas: list[int | None] = []
    for observation in values:
        applied = apply_observation(prior, "binding", observation)
        assert applied is not None
        prior, row = applied
        deltas.append(row.deltas["input_tokens"])
    assert deltas == [None, 100, 20, -10]
    assert apply_observation(prior, "binding", values[1]) is None


@pytest.mark.asyncio
async def test_usage_unknown_counts_remain_null_and_subagent_grain_distinct(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    child = turn(id_="child", child="sub")
    child["usage"] = {"output_tokens": 7}
    transport.responses["GET", "/agents/sessions/s/turns"] = page(turn(), child)
    observations = await driver.usage.reconcile(SCOPE, REF)
    assert observations[0].input_tokens is None and observations[1].input_tokens is None
    assert observations[1].thread_id == "sub" and observations[1].grain == "turn"
    assert observations[0].id != observations[1].id and observations[1].basis == "cumulative"


@pytest.mark.asyncio
async def test_usage_completeness_changes_receive_new_revision(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    from mux.state.usage_ledger import apply_observation

    native = turn()
    transport.responses["GET", "/agents/sessions/s/turns"] = page(native)
    first = (await driver.usage.reconcile(SCOPE, REF))[0]
    assert first.completeness == "unknown" and first.revision == 1
    applied = apply_observation(None, "binding", first)
    assert applied is not None
    native["usage"] = {}
    second = (await driver.usage.reconcile(SCOPE, REF))[0]
    assert second.completeness == "partial" and second.revision == 2
    assert second.input_tokens is None and second.output_tokens is None
    assert apply_observation(applied[0], "binding", second) is not None
    replay = (await driver.usage.reconcile(SCOPE, REF))[0]
    assert replay.revision == second.revision


@pytest.mark.asyncio
async def test_overlapping_snapshot_pages_choose_final_content_and_disconnect_cannot_commit() -> (
    None
):

    class Paginated(FakeTransport):
        break_second = False

        async def request(
            self,
            method: Method,
            path: str,
            *,
            body: Object | None = None,
            query: Mapping[str, str | int] | None = None,
            key: str | None = None,
        ) -> Object:
            if path.endswith("/items"):
                self.calls.append((method, path, body, query))
                self.request_keys.append(key)
                if query and query.get("after") == "item":
                    if self.break_second:
                        self.sources[-1].wait.set()
                        import asyncio

                        await asyncio.sleep(0)
                    return page(message(), message(id_="another"))
                return page(message("partial", status="in_progress"), more=True)
            return await super().request(method, path, body=body, query=query, key=key)

    transport = Paginated(
        hold=True,
        responses={
            ("GET", "/agents/sessions/s"): native_session(),
            ("GET", "/agents/sessions/s/turns"): page(turn()),
        },
    )
    journal = MemoryRecoveryJournal()
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=journal,
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
    )
    await driver.events.reconcile(SCOPE, REF)
    prior = await journal.read(REF)
    assert prior is not None
    saved = [e for e in prior if e.type == "agent.message"]
    assert len(saved) == 2 and all(e.payload["complete"] is True for e in saved)
    assert saved[0].payload["content"] == [{"type": "text", "text": "done"}]
    transport.break_second = True
    with pytest.raises(ProviderError):
        await driver.events.reconcile(SCOPE, REF)
    assert await journal.read(REF) == prior


@pytest.mark.asyncio
async def test_usage_malformed_does_not_allocate_revision(
    driver: OpenAIDriver, transport: FakeTransport
) -> None:
    native = turn()
    native["usage"] = {"input_tokens": -1}
    transport.responses["GET", "/agents/sessions/s/turns"] = page(native)
    with pytest.raises(ProviderError):
        await driver.usage.reconcile(SCOPE, REF)
    native["usage"] = {"input_tokens": 10}
    transport.responses["GET", "/agents/sessions/s/turns"] = page(native)
    assert (await driver.usage.reconcile(SCOPE, REF))[0].revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["requires_action", "failed", "environment.failed"])
async def test_recovery_reduces_buffered_lifecycle_and_actions_atomically(kind: str) -> None:
    journal = MemoryRecoveryJournal()
    native = native_session("in_progress")
    during_pagination = asyncio.Event()

    class RaceSource(Source):
        async def __anext__(self) -> Object:
            await during_pagination.wait()
            return await super().__anext__()

    class PaginationRace(FakeTransport):
        async def open_stream(self, path: str) -> AsyncIterator[Object]:
            self.calls.append(("GET", path, None, None))
            self.request_keys.append(None)
            source = RaceSource(self.stream_values, hold=True)
            self.sources.append(source)
            return source

        async def request(
            self,
            method: Method,
            path: str,
            *,
            body: Object | None = None,
            query: Mapping[str, str | int] | None = None,
            key: str | None = None,
        ) -> Object:
            if path.endswith("/items"):
                during_pagination.set()
                await asyncio.sleep(0)
            return await super().request(method, path, body=body, query=query, key=key)

    transport = PaginationRace(
        hold=True,
        responses={
            ("GET", "/agents/sessions/s"): native,
            ("GET", "/agents/sessions/s/turns"): page(turn("in_progress")),
            ("GET", "/agents/sessions/s/items"): page(),
        },
    )
    live = native_session("requires_action" if kind == "requires_action" else "failed")
    live["required_actions"] = (
        [
            {
                "type": "function_call",
                "call_id": "call",
                "turn_id": "root",
                "name": "tool",
                "arguments": {},
            }
        ]
        if kind == "requires_action"
        else []
    )
    transport.stream_values = [
        {"type": "agent.session." + kind, "event_id": "live", "session": live}
    ]
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=journal,
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
    )
    projection = await driver.events.reconcile(SCOPE, REF)
    published = await journal.read(REF)
    assert published is not None and published[-1].id == projection.cursor
    assert [e.sequence for e in published] == list(range(len(published)))
    if kind == "requires_action":
        assert projection.state == "requires_action" and projection.active_root_turn == "root"
        assert [action.id for action in projection.required_actions] == ["call"]
        required = [e.typed_payload() for e in published if e.type == "session.requires_action"]
        assert required and isinstance(required[0], RequiresActionPayload)
        assert required[0].actions == projection.required_actions
    else:
        assert projection.state == "terminated" and projection.active_root_turn is None
        assert projection.required_actions == ()
        assert any(e.type == "session.status_terminated" for e in published)
    assert transport.sources[0].closed


@pytest.mark.asyncio
async def test_recovery_terminal_clears_stale_snapshot_actions() -> None:
    native = native_session("requires_action")
    native["required_actions"] = [{"type": "function_call", "call_id": "call", "turn_id": "root"}]
    transport = FakeTransport(
        hold=True,
        responses={
            ("GET", "/agents/sessions/s"): native,
            ("GET", "/agents/sessions/s/turns"): page(turn("waiting")),
            ("GET", "/agents/sessions/s/items"): page(),
        },
        stream_values=[event("turn.completed", "end", turn=turn())],
    )
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
    )
    projection = await driver.events.reconcile(SCOPE, REF)
    assert (projection.state, projection.active_root_turn, projection.required_actions) == (
        "idle",
        None,
        (),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["action_then_end", "end_then_action", "idle_new_action"])
async def test_recovery_uses_documented_nested_action_turn_identity(case: str) -> None:
    action = required_action_event()
    assert set(action) == {"type", "event_id", "session"}
    snapshot = object_json(action["session"]) if case != "idle_new_action" else native_session()
    ended = event("turn.completed", "end", turn=turn())
    buffered = {
        "action_then_end": [action, ended],
        "end_then_action": [ended, action],
        "idle_new_action": [action],
    }[case]
    journal = MemoryRecoveryJournal()
    transport = FakeTransport(
        hold=True,
        responses={
            ("GET", "/agents/sessions/s"): snapshot,
            ("GET", "/agents/sessions/s/turns"): page(turn("waiting")),
            ("GET", "/agents/sessions/s/items"): page(),
        },
        stream_values=buffered,
    )
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=journal,
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
    )
    projection = await driver.events.reconcile(SCOPE, REF)
    published = await journal.read(REF)
    assert published is not None and projection.cursor == published[-1].id
    assert [e.sequence for e in published] == list(range(len(published)))
    action_events = [e for e in published if e.type == "session.requires_action"]
    assert all(e.turn_id == "root" for e in action_events)
    if case == "idle_new_action":
        assert projection.state == "requires_action" and projection.active_root_turn == "root"
        assert [a.id for a in projection.required_actions] == ["call"]
        assert len(action_events) == 1
        payload = action_events[0].typed_payload()
        assert isinstance(payload, RequiresActionPayload)
        assert payload.actions == projection.required_actions
    else:
        assert (projection.state, projection.active_root_turn, projection.required_actions) == (
            "idle",
            None,
            (),
        )
        assert sum(e.type == "session.turn_ended" for e in published) == 1
        if case == "end_then_action":
            assert action_events == []
    assert transport.sources[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "empty", "nonstring", "mixed", "outer_conflict"])
async def test_invalid_action_turn_identity_cannot_replace_journal(invalid: str) -> None:
    action = required_action_event()
    snapshot = object_json(action["session"])
    actions = list(objects(snapshot["required_actions"]))
    if invalid == "missing":
        actions[0].pop("turn_id")
    elif invalid == "empty":
        actions[0]["turn_id"] = ""
    elif invalid == "nonstring":
        actions[0]["turn_id"] = 42
    elif invalid == "mixed":
        actions.append({**actions[0], "call_id": "other", "turn_id": "other-root"})
    else:
        action["turn_id"] = "other-root"
    snapshot["required_actions"] = [value for value in actions]
    action["session"] = snapshot
    journal = MemoryRecoveryJournal()
    transport = FakeTransport(
        hold=True,
        responses={
            ("GET", "/agents/sessions/s"): native_session("in_progress"),
            ("GET", "/agents/sessions/s/turns"): page(turn()),
            ("GET", "/agents/sessions/s/items"): page(),
        },
        stream_values=[action],
    )
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=journal,
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
    )
    old = await driver.events.list(SCOPE, REF, page=PageRequest())
    transport.responses[("GET", "/agents/sessions/s/turns")] = page(turn("waiting"))
    with pytest.raises(ProviderError) as refused:
        await driver.events.reconcile(SCOPE, REF)
    assert refused.value.category == "upstream"
    assert await journal.read(REF) == old.data
    assert transport.sources[0].closed
    transport.responses[("GET", "/agents/sessions/s/turns")] = page(turn())
    after = await driver.events.list(SCOPE, REF, page=PageRequest(cursor=old.data[-1].id))
    assert after.data == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list", "reconcile"])
async def test_terminal_conflict_after_restart_preserves_journal_and_cursor(method: str) -> None:
    journal = MemoryRecoveryJournal()
    transport = FakeTransport(
        hold=True,
        responses={
            ("GET", "/agents/sessions/s"): native_session(),
            ("GET", "/agents/sessions/s/turns"): page(turn()),
            ("GET", "/agents/sessions/s/items"): page(message()),
        },
    )

    def restart() -> OpenAIDriver:
        return OpenAIDriver(
            transport,
            account_scope_id="project",
            journal=journal,
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda s, k, i: s == SCOPE,
        )

    before = await restart().events.list(SCOPE, REF, page=PageRequest(limit=1))
    saved = await journal.read(REF)
    transport.responses["GET", "/agents/sessions/s/turns"] = page(turn("cancelled"))
    with pytest.raises(ProviderError):
        if method == "list":
            await restart().events.list(SCOPE, REF, page=PageRequest())
        else:
            await restart().events.reconcile(SCOPE, REF)
    assert await journal.read(REF) == saved
    transport.responses["GET", "/agents/sessions/s/turns"] = page(turn())
    after = await restart().events.list(SCOPE, REF, page=PageRequest(limit=1))
    assert after == before
    assert (
        await restart().events.list(SCOPE, REF, page=PageRequest(cursor=before.next_cursor))
    ).data


@pytest.mark.asyncio
async def test_buffered_terminal_conflict_cannot_replace_durable_outcome() -> None:
    journal = MemoryRecoveryJournal()
    transport = FakeTransport(
        hold=True,
        responses={
            ("GET", "/agents/sessions/s"): native_session(),
            ("GET", "/agents/sessions/s/turns"): page(turn()),
            ("GET", "/agents/sessions/s/items"): page(),
        },
    )
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=journal,
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
    )
    await driver.events.list(SCOPE, REF, page=PageRequest())
    before = await journal.read(REF)
    transport.stream_values = [event("turn.cancelled", "conflict", turn=turn("cancelled"))]
    with pytest.raises(ProviderError):
        await driver.events.reconcile(SCOPE, REF)
    assert await journal.read(REF) == before

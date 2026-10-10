"""Pinned SDK regression for the exact documented Agents mcp_call item."""

from __future__ import annotations

import json

import httpx
import pytest
from mux.contracts.events import Event, TextPart, ToolResultPayload, ToolUsePayload
from mux.contracts.ids import PageRequest
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.normalize import EventNormalizer
from mux.drivers.openai.transport import Object, SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError
from openai import AsyncOpenAI

from .conftest import REF, SCOPE, event, page, turn


def mcp(*, status: str = "completed") -> Object:
    return {
        "id": "mcp-context",
        "type": "mcp_call",
        "turn_id": "root",
        "name": "list_events",
        "server_label": "daimon-mcp",
        "arguments": {"handle": "fixture"},
        "output": {"context": "fixture"},
        "error": None,
        "status": status,
    }


def driver_for(sdk: AsyncOpenAI) -> OpenAIDriver:
    return OpenAIDriver(
        SDKTransport(sdk),
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda scope, kind, identity: scope == SCOPE,
    )


def assert_pair(events: list[Event], *, authority: str) -> None:
    assert [e.type for e in events] == ["agent.tool_use", "agent.tool_result"]
    assert [e.sequence for e in events] == [0, 1]
    assert len({e.id for e in events}) == len({e.item_id for e in events}) == 2
    assert all(e.authority == authority and e.turn_id == "root" for e in events)
    use, result = (e.typed_payload() for e in events)
    assert isinstance(use, ToolUsePayload) and isinstance(result, ToolResultPayload)
    assert use.call_id == result.call_id == "mcp-context"
    assert use.executor == "mcp" and use.mcp_server == "daimon-mcp"
    assert use.tool_name == "list_events" and use.input == {"handle": "fixture"}
    assert result.content == (TextPart(text='{"context": "fixture"}'),)
    assert not result.is_error
    assert all(e.native.event_type == "agent.session.turn.item.done" for e in events)


@pytest.mark.asyncio
async def test_sdk_sse_done_only_mcp_item_produces_both_records() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/agents/sessions/s/events"
        assert request.url.params["stream"] == "true"
        assert request.headers["Accept"] == "text/event-stream"
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        raw = event("turn.item.done", "native-done", item=mcp(), turn_id="root")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(raw) + "\n\n",
        )

    async with AsyncOpenAI(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        events = [e async for e in driver_for(sdk).events.stream(SCOPE, REF)]
    assert_pair(events, authority="record")


@pytest.mark.asyncio
async def test_sdk_history_overlap_refresh_and_cursor_preserve_both_mcp_records() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        assert request.url.params["order"] == "asc"
        if request.url.path.endswith("/turns"):
            return httpx.Response(200, json=page())
        assert request.url.path.endswith("/items")
        # Overlapping items are a real pagination concern, not two executions.
        return httpx.Response(200, json=page(mcp(), mcp()))

    async with AsyncOpenAI(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = driver_for(sdk)
        first = await driver.events.list(SCOPE, REF, page=PageRequest(limit=1))
        assert first.has_more and first.next_cursor == first.data[0].id
        second = await driver.events.list(SCOPE, REF, page=PageRequest(cursor=first.next_cursor))
        assert not second.has_more
        assert_pair(list(first.data + second.data), authority="reconciled")
        again = await driver.events.list(SCOPE, REF, page=PageRequest())
        assert again.data == first.data + second.data


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_unsuccessful_mcp_status_is_error_even_without_native_error(status: str) -> None:
    values = EventNormalizer("s").saved_item_batch(mcp(status=status))
    result = values[-1].typed_payload()
    assert isinstance(result, ToolResultPayload) and result.is_error
    assert "error" not in values[-1].payload


def test_mcp_preview_is_not_result_and_done_supersedes_it_once() -> None:
    normalizer = EventNormalizer("s")
    preview = normalizer.normalize_batch(
        event("turn.item.added", "add", item=mcp(status="in_progress"))
    )
    assert len(preview) == 1 and preview[0].authority == "preview"
    final = normalizer.normalize_batch(event("turn.item.done", "done", item=mcp()))
    assert len(final) == 2 and all(e.authority == "record" for e in final)
    assert normalizer.saved_item_batch(mcp()) == ()
    assert normalizer.normalize_batch(event("turn.item.done", "done", item=mcp())) == ()


def test_child_mcp_keeps_child_authority_and_cannot_be_root_tool_evidence() -> None:
    normalizer = EventNormalizer("s")
    normalizer.normalize(
        event("turn.in_progress", "child-start", turn=turn(id_="root", child="child"))
    )
    values = normalizer.normalize_batch(event("turn.item.done", "child-mcp", item=mcp()))
    assert len(values) == 1 and values[0].thread_id == "child"
    assert values[0].type.startswith("agent.thread.")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["foreign_session", "foreign_turn", "status", "arguments"])
async def test_sdk_malformed_mcp_item_is_owned_error_without_body(fault: str) -> None:
    item = mcp()
    raw = event("turn.item.done", "native-done", item=item, turn_id="root")
    if fault == "foreign_session":
        raw["session_id"] = "foreign"
    elif fault == "foreign_turn":
        raw["turn_id"] = "foreign"
    elif fault == "status":
        item["status"] = "unrecognized"
    else:
        item["arguments"] = "sensitive-invalid-json"
    raw["item"] = item

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(raw) + "\n\n",
        )

    async with AsyncOpenAI(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        with pytest.raises(ProviderError) as caught:
            _ = [e async for e in driver_for(sdk).events.stream(SCOPE, REF)]
    assert caught.value.native_code == "malformed_event"
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_sdk_mcp_failed_tool_output_cannot_be_success_with_completed_item() -> None:
    item = mcp()
    item["arguments"] = '{"handle":"fixture"}'
    item["output"] = {"isError": True, "content": [{"type": "text", "text": "failed"}]}

    def handle(request: httpx.Request) -> httpx.Response:
        raw = event("turn.item.done", "native-done", item=item, turn_id="root")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(raw) + "\n\n",
        )

    async with AsyncOpenAI(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        events = [e async for e in driver_for(sdk).events.stream(SCOPE, REF)]
    use, result = (e.typed_payload() for e in events)
    assert isinstance(use, ToolUsePayload) and use.input == {"handle": "fixture"}
    assert isinstance(result, ToolResultPayload) and result.is_error


@pytest.mark.asyncio
async def test_sdk_native_command_missing_exit_code_never_proves_success() -> None:
    item: Object = {
        "id": "command",
        "type": "command_execution",
        "turn_id": "root",
        "command": "printf marker",
        "cwd": "/fixture",
        "duration_ms": None,
        "exit_code": None,
        "output": "marker",
        "status": "completed",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        raw = event("turn.item.done", "native-done", item=item, turn_id="root")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(raw) + "\n\n",
        )

    async with AsyncOpenAI(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        events = [e async for e in driver_for(sdk).events.stream(SCOPE, REF)]
    use, result = (e.typed_payload() for e in events)
    assert isinstance(use, ToolUsePayload) and use.executor == "agent" and use.tool_name == "bash"
    assert isinstance(result, ToolResultPayload) and result.is_error


@pytest.mark.asyncio
async def test_sdk_malformed_saved_mcp_item_is_redacted_and_never_published() -> None:
    item = mcp()
    item["arguments"] = "sensitive-invalid-json"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/turns"):
            return httpx.Response(200, json=page())
        return httpx.Response(200, json=page(item))

    journal = MemoryRecoveryJournal()
    async with AsyncOpenAI(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=journal,
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda scope, kind, identity: scope == SCOPE,
        )
        with pytest.raises(ProviderError) as caught:
            await driver.events.list(SCOPE, REF, page=PageRequest())
    assert caught.value.native_code == "malformed_snapshot"
    assert "sensitive" not in str(caught.value) and caught.value.__cause__ is None
    assert await journal.read(REF) is None

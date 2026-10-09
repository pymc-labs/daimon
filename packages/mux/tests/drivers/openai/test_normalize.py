from __future__ import annotations

import pytest
from mux.contracts.events import AgentMessagePayload, RequiresActionPayload, TextPart
from mux.drivers.openai.normalize import EventNormalizer
from mux.drivers.openai.transport import Object
from mux.drivers.openai.turn import merge_history

from .conftest import event, message, native_session, turn


def test_root_truth_never_idle_eof_or_child_completion() -> None:
    normalizer = EventNormalizer("s")
    child = normalizer.normalize(
        event("turn.completed", "child", turn=turn(id_="child", child="sub"))
    )
    idle = normalizer.normalize(event("idle", "idle", session=native_session()))
    root = normalizer.normalize(event("turn.completed", "root", turn=turn()))
    duplicate = normalizer.normalize(event("turn.completed", "duplicate", turn=turn()))
    assert child is not None and child.type.startswith("agent.thread.")
    assert idle is not None and idle.type.startswith("native.")
    assert root is not None and root.type == "session.turn_ended" and root.turn_id == "root"
    assert duplicate is None
    assert normalizer.normalize(event("turn.completed", "root", turn=turn())) is None


def test_terminal_requires_positive_root_identity() -> None:
    native = turn()
    native.pop("subagent_id")
    with pytest.raises(ValueError, match="root identity"):
        EventNormalizer("s").normalize(event("turn.completed", "e", turn=native))


def test_saved_terminal_outweighs_older_buffered_running_but_allows_next_root() -> None:
    normalizer = EventNormalizer("s")
    ended = normalizer.saved_turn(turn())
    stale = normalizer.normalize(event("turn.in_progress", "old", turn=turn("in_progress")))
    next_root = normalizer.normalize(
        event("turn.in_progress", "next", turn=turn("in_progress", id_="next-root"))
    )
    assert ended is not None and ended.type == "session.turn_ended"
    assert stale is None
    assert next_root is not None and next_root.turn_id == "next-root"
    assert next_root.type == "session.status_running"


@pytest.mark.parametrize("guard", ["retained_normalizer", "history_merge"])
def test_each_durable_terminal_guard_refuses_changed_outcome(guard: str) -> None:
    previous = EventNormalizer("s").saved_turn(turn())
    assert previous is not None
    with pytest.raises(ValueError, match="conflicting root outcomes"):
        if guard == "retained_normalizer":
            EventNormalizer("s", prior=(previous,)).saved_turn(turn("cancelled"))
        else:
            incoming = EventNormalizer("s").saved_turn(turn("cancelled"))
            assert incoming is not None
            merge_history((previous,), (incoming,))


def test_history_merge_preserves_published_terminal_cursor() -> None:
    previous = EventNormalizer("s").normalize(event("turn.completed", "published", turn=turn()))
    incoming = EventNormalizer("s").saved_turn(turn())
    assert previous is not None and incoming is not None and previous.id != incoming.id
    assert merge_history((previous,), (incoming,)) == (previous,)


def test_preview_never_authoritative_and_final_replaces_buffer() -> None:
    n = EventNormalizer("s")
    preview = n.normalize(
        event(
            "turn.output_text.delta",
            "delta",
            item_id="item",
            turn_id="root",
            content_index=0,
            delta="d",
        )
    )
    final = n.normalize(event("turn.item.done", "final", item=message(), turn_id="root"))
    late = n.normalize(
        event("turn.output_text.delta", "late", item_id="item", content_index=0, delta="wrong")
    )
    duplicate = n.saved_item(message())
    assert preview is not None and preview.authority == "preview"
    assert final is not None and final.authority == "record"
    assert isinstance(final.typed_payload(), AgentMessagePayload)
    assert final.payload["content"] == [{"type": "text", "text": "done"}]
    assert duplicate is None and late is None


def test_output_text_done_does_not_invent_complete_message() -> None:
    e = EventNormalizer("s").normalize(
        event("turn.output_text.done", "done", item_id="item", content_index=1, text="second part")
    )
    assert e is not None and e.authority == "preview" and e.type.startswith("native.")


def test_required_actions_preserve_function_and_environment_kinds() -> None:
    session = native_session("requires_action")
    session["required_actions"] = [
        {
            "type": "function_call",
            "call_id": "call",
            "turn_id": "root",
            "name": "tool",
            "arguments": {"n": 1},
        },
        {"type": "environment_connection", "environment_id": "env"},
        {
            "type": "computer_use_approval_request",
            "request_id": "browser",
            "turn_id": "root",
            "request": {"type": "browser_authentication"},
        },
    ]
    e = EventNormalizer("s").normalize(
        {"type": "agent.session.requires_action", "event_id": "e", "session": session}
    )
    assert e is not None and e.turn_id == "root"
    payload = e.typed_payload()
    assert isinstance(payload, RequiresActionPayload)
    assert [a.kind for a in payload.actions] == [
        "function_result",
        "environment_connection",
        "native",
    ]
    assert payload.actions[0].call_id == "call" and payload.actions[0].payload["turn_id"] == "root"


def test_saved_item_keeps_multiple_parts_phase_and_tool_pairing() -> None:
    item = message()
    item["content"] = [
        {"type": "output_text", "text": "one"},
        {"type": "output_text", "text": "two"},
    ]
    n = EventNormalizer("s")
    e = n.saved_item(item)
    assert e is not None
    p = e.typed_payload()
    assert isinstance(p, AgentMessagePayload) and p.content == (
        TextPart(text="one"),
        TextPart(text="two"),
    )
    assert p.phase == "final_answer"
    output = n.saved_item(
        {
            "id": "out",
            "turn_id": "root",
            "type": "function_call_output",
            "call_id": "call",
            "status": "completed",
            "output": "result",
            "error": None,
        }
    )
    assert (
        output is not None
        and output.type == "agent.tool_result"
        and output.payload["call_id"] == "call"
    )


@pytest.mark.parametrize("reconciled", [False, True])
def test_conflicting_terminal_and_foreign_session_fail(reconciled: bool) -> None:
    n = EventNormalizer("s")
    n.normalize(event("turn.completed", "e", turn=turn()))
    with pytest.raises(ValueError, match="conflicting"):
        n.normalize(event("turn.cancelled", "other", turn=turn("cancelled")), reconciled=reconciled)
    with pytest.raises(ValueError, match="another session"):
        n.normalize(event("idle", "foreign", session_id="foreign"))


def test_native_error_normalizes_category_without_upstream_message() -> None:
    raw: Object = {
        "type": "error",
        "event_id": "e",
        "session_id": "s",
        "error": {"type": "server_error", "code": "rate_limit_exceeded", "message": "sensitive"},
    }
    result = EventNormalizer("s").normalize(raw)
    assert result is not None and result.type == "session.error"
    assert result.payload["category"] == "rate_limited" and result.payload["message"] is None


def test_foreign_native_turn_cannot_end_local_root() -> None:
    native = turn()
    native["session_id"] = "foreign"
    with pytest.raises(ValueError, match="another session"):
        EventNormalizer("s").normalize(event("turn.completed", "e", turn=native))

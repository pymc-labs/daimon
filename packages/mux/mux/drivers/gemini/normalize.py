"""Saved steps are authoritative; streaming deltas are display previews only."""

from datetime import datetime

from pydantic import JsonValue

from mux.contracts.events import Event, NativeProvenance, RequiredAction, TurnOutcome
from mux.contracts.ids import ResourceRef
from mux.drivers.gemini.transport import API_REVISION, Object, object_value, string

OUTCOMES: dict[str, TurnOutcome] = {
    "completed": "completed",
    "cancelled": "interrupted",
    "failed": "errored",
    "incomplete": "terminated",
    "budget_exceeded": "terminated",
}


def event(
    session: ResourceRef,
    interaction_id: str,
    root: str,
    name: str,
    payload: Object,
    *,
    identity: str,
    now: datetime,
    preview: bool = False,
    native_type: str | None = None,
) -> Event:
    return Event(
        id=f"gemini:{interaction_id}:{identity}",
        session_id=session.id,
        sequence=0,
        type=name,
        turn_id=root,
        observed_at=now,
        authority="preview" if preview else "record",
        payload=payload,
        native=NativeProvenance(
            provider="gemini",
            api_revision=API_REVISION,
            event_id=identity,
            event_type=native_type,
            ordering_domain=interaction_id,
        ),
    )


def actions(raw: Object) -> tuple[RequiredAction, ...]:
    if raw.get("status") != "requires_action":
        return ()
    steps = raw.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("expected saved steps")
    executed = {
        string(step["call_id"])
        for value in steps
        if (step := object_value(value)).get("type") == "function_result"
    }
    pending: list[RequiredAction] = []
    for value in steps:
        step = object_value(value)
        if step.get("type") == "function_call":
            call = string(step["id"])
            if call in executed:
                continue
            pending.append(
                RequiredAction(
                    id=call,
                    call_id=call,
                    kind="function_result",
                    payload={"name": string(step["name"])},
                    native_type="function_call",
                )
            )
    # Never hide an action the harness cannot execute locally.
    if not pending:
        pending.append(
            RequiredAction(
                id=f"{string(raw['id'])}:action",
                kind="native",
                native_type="requires_action",
            )
        )
    return tuple(pending)


def saved_events(
    raw: Object,
    session: ResourceRef,
    *,
    root: str,
    now: datetime,
) -> tuple[Event, ...]:
    interaction = string(raw["id"])
    result: list[Event] = []
    steps = raw.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("expected saved steps")
    for index, value in enumerate(steps):
        # An in-progress saved step may still be open. Its position is stable
        # but its content is not; only paused/terminal snapshots enter history.
        if raw.get("status") == "in_progress":
            break
        step = object_value(value)
        kind = string(step["type"])
        identity = str(step.get("id") or f"step:{index}")
        payload: Object
        match kind:
            case "model_output":
                content = step.get("content", [])
                if not isinstance(content, list):
                    raise ValueError("expected saved content")
                parts: list[JsonValue] = []
                for part in content:
                    obj = object_value(part)
                    if obj.get("type") == "text":
                        parts.append({"type": "text", "text": string(obj["text"])})
                    else:
                        parts.append(
                            {
                                "type": "native",
                                "namespace": "gemini.content",
                                "version": 1,
                                "payload": obj,
                            }
                        )
                name = "agent.message"
                payload = {"item_id": identity, "content": parts}
            case "function_call" | "code_execution_call" | "mcp_server_tool_call":
                name = "agent.tool_use"
                payload = {
                    "call_id": string(step["id"]),
                    "tool_name": "code_execution"
                    if kind == "code_execution_call"
                    else string(step["name"]),
                    "input": object_value(step.get("arguments", {})),
                    "executor": "host"
                    if kind == "function_call"
                    else ("mcp" if kind == "mcp_server_tool_call" else "agent"),
                    "mcp_server": step.get("server_name"),
                }
            case "function_result" | "code_execution_result" | "mcp_server_tool_result":
                name = "agent.tool_result"
                payload = {
                    "call_id": string(step["call_id"]),
                    "content": [{"type": "text", "text": step["result"]}]
                    if isinstance(step.get("result"), str)
                    else [
                        {
                            "type": "native",
                            "namespace": "gemini.tool_result",
                            "version": 1,
                            "payload": step,
                        }
                    ],
                    "is_error": step.get("is_error", False),
                }
            case "user_input":
                # Inputs are journaled at acceptance, not duplicated by GET.
                continue
            case _:
                name, payload = f"native.gemini.{kind}", step
        result.append(
            event(
                session,
                interaction,
                root,
                name,
                payload,
                identity=identity,
                now=now,
                native_type=kind,
            )
        )
    pending = actions(raw)
    status = string(raw["status"])
    if pending:
        result.append(
            event(
                session,
                interaction,
                root,
                "session.requires_action",
                {
                    "actions": [a.model_dump(mode="json") for a in pending],
                },
                identity="actions",
                now=now,
            )
        )
    elif status in OUTCOMES:
        result.append(
            event(
                session,
                interaction,
                root,
                "session.turn_ended",
                {
                    "root_turn_id": root,
                    "outcome": OUTCOMES[status],
                    "native_reason": status,
                },
                identity=f"root:{root}:end",
                now=now,
            )
        )
    elif status == "in_progress":
        result.append(
            event(
                session,
                interaction,
                root,
                "session.status_running",
                {
                    "root_turn_id": root,
                },
                identity="running",
                now=now,
            )
        )
    else:
        raise ValueError("unknown interaction status")
    return tuple(result)

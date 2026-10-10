"""Authoritative items/root outcomes, display previews and explicit gaps."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import JsonValue

from mux.contracts.events import (
    AgentMessageDeltaPayload,
    AgentMessagePayload,
    ContentPart,
    Event,
    ImagePart,
    NativePart,
    NativeProvenance,
    RequiredAction,
    RequiresActionPayload,
    SessionErrorPayload,
    StatusRunningPayload,
    StatusTerminatedPayload,
    TextPart,
    ToolResultPayload,
    ToolUsePayload,
    TurnEndedPayload,
)
from mux.drivers.openai._common import objects, text
from mux.drivers.openai.transport import Object, error_category, object_json

SCHEMA_DATE = "2026-10-09"


def parts(value: JsonValue) -> tuple[ContentPart, ...]:
    result: list[ContentPart] = []
    for part in objects(value):
        if part.get("type") in ("input_text", "output_text"):
            value = part["text"]
            if not isinstance(value, str):
                raise ValueError("invalid text")
            result.append(TextPart(text=value))
        elif part.get("type") == "input_image":
            url = text(part["image_url"])
            if url.startswith("data:") and ";base64," in url:
                media, data = url[5:].split(";base64,", 1)
                result.append(ImagePart(media_type=media, data_base64=data))
            else:
                result.append(NativePart(namespace="openai.content", version=1, payload=part))
        else:
            result.append(NativePart(namespace="openai.content", version=1, payload=part))
    return tuple(result)


def required_actions(raw: Object) -> tuple[RequiredAction, ...]:
    result: list[RequiredAction] = []
    for action in objects(raw.get("required_actions", [])):
        native_type = text(action["type"])
        if native_type == "function_call":
            id_ = text(action["call_id"])
            result.append(
                RequiredAction(
                    id=id_,
                    kind="function_result",
                    call_id=id_,
                    payload=action,
                    native_type=native_type,
                )
            )
        elif native_type == "environment_connection":
            result.append(
                RequiredAction(
                    id=text(action["environment_id"]),
                    kind="environment_connection",
                    payload=action,
                    native_type=native_type,
                )
            )
        else:
            # Browser authentication is not a generic allow/deny confirmation.
            id_ = text(action.get("request_id") or action.get("id"))
            result.append(
                RequiredAction(id=id_, kind="native", payload=action, native_type=native_type)
            )
    return tuple(result)


def required_action_turn(actions: Sequence[RequiredAction]) -> str | None:
    """Native session events put turn identity inside the required actions.

    Environment reconnection has no turn ID. Function and approval requests
    must identify one turn; conflicting identities cannot route root work.
    """
    turns: set[str] = set()
    for action in actions:
        if (
            action.native_type in ("function_call", "computer_use_approval_request")
            or action.payload.get("turn_id") is not None
        ):
            turns.add(text(action.payload["turn_id"]))
    if len(turns) > 1:
        raise ValueError("required actions identify conflicting turns")
    return next(iter(turns), None)


class EventNormalizer:
    def __init__(self, session_id: str, *, prior: Sequence[Event] = ()) -> None:
        self.session_id = session_id
        self.sequence = 0
        self._seen: set[str] = set()
        self._terminals: dict[str, str] = {}
        self._children: dict[str, str] = {}
        self._final_items: set[str] = set()
        for event in prior:
            if event.session_id != session_id:
                raise ValueError("journal belongs to another session")
            if event.type == "session.turn_ended":
                payload = event.typed_payload()
                if (
                    not isinstance(payload, TurnEndedPayload)
                    or event.turn_id != payload.root_turn_id
                ):
                    raise ValueError("journal terminal lacks root identity")
                previous = self._terminals.get(payload.root_turn_id)
                if previous is not None and previous != payload.outcome:
                    raise ValueError("conflicting journal root outcomes")
                self._terminals[payload.root_turn_id] = payload.outcome

    def terminal(self, turn_id: str) -> bool:
        return turn_id in self._terminals

    def normalize_batch(self, raw: Object, *, reconciled: bool = False) -> tuple[Event, ...]:
        """MCP and command items contain both invocation and execution result.

        Keep its native provenance while giving each neutral record its own
        stable item identity. A running item is only a preview, and child items
        retain their thread event rather than becoming root tool evidence.
        """
        item = object_json(raw.get("item") or {})
        combined = raw.get("type") in (
            "agent.session.turn.item.added",
            "agent.session.turn.item.done",
        ) and item.get("type") in ("mcp_call", "command_execution")
        if combined:
            item_turn = text(item["turn_id"])
            turn = object_json(raw.get("turn") or {})
            outer_turn = turn.get("id") or raw.get("turn_id")
            if outer_turn is not None and outer_turn != item_turn:
                raise ValueError("item belongs to another turn")
            raw = {**raw, "turn_id": item_turn}
            if item.get("status") not in ("in_progress", "completed", "failed", "incomplete"):
                raise ValueError("invalid tool status")
        event = self.normalize(raw, reconciled=reconciled)
        if event is None:
            return ()
        if not combined or event.thread_id is not None:
            return (event,)
        mcp = item["type"] == "mcp_call"
        if not mcp:
            text(item["command"])
            if item["cwd"] is not None and not isinstance(item["cwd"], str):
                raise ValueError("invalid command directory")
            exit_code = item["exit_code"]
            if exit_code is not None and (
                isinstance(exit_code, bool) or not isinstance(exit_code, int)
            ):
                raise ValueError("invalid command exit code")
        arguments = item["arguments"] if mcp else {"command": item["command"], "cwd": item["cwd"]}
        if isinstance(arguments, str):
            arguments = object_json(json.loads(arguments))
        use = ToolUsePayload(
            call_id=text(item["id"]),
            tool_name=text(item["name"]) if mcp else "bash",
            input=object_json(arguments),
            executor="mcp" if mcp else "agent",
            mcp_server=text(item["server_label"]) if mcp else None,
        )
        invocation = Event.model_validate(
            {
                **event.model_dump(),
                "id": event.id + ":use",
                "item_id": text(item["id"]) + ":use",
                "type": "agent.tool_use",
                "payload": object_json(use.model_dump(mode="json")),
            }
        )
        if event.authority == "preview":
            return (invocation,)
        output = item["output"]
        content = (
            ()
            if output is None
            else (
                TextPart(
                    text=output if isinstance(output, str) else json.dumps(output, sort_keys=True)
                ),
            )
        )
        result = ToolResultPayload(
            call_id=use.call_id,
            content=content,
            is_error=(mcp and item["error"] is not None)
            or item["status"] != "completed"
            or (not mcp and item.get("exit_code") != 0)
            or (mcp and isinstance(output, dict) and output.get("isError") is True),
        )
        completed = Event.model_validate(
            {
                **event.model_dump(),
                "id": event.id + ":result",
                "item_id": text(item["id"]) + ":result",
                "sequence": self.sequence,
                "type": "agent.tool_result",
                "payload": object_json(result.model_dump(mode="json")),
            }
        )
        self.sequence += 1
        return (invocation, completed)

    def saved_item_batch(self, item: Object) -> tuple[Event, ...]:
        if item.get("type") not in ("mcp_call", "command_execution"):
            event = self.saved_item(item)
            return (event,) if event is not None else ()
        return self.normalize_batch(
            {
                "type": "agent.session.turn.item.done",
                "event_id": "openai:item:" + text(item["id"]),
                "session_id": self.session_id,
                "turn_id": item.get("turn_id"),
                "item": item,
            },
            reconciled=True,
        )

    def normalize(self, raw: Object, *, reconciled: bool = False) -> Event | None:
        kind = text(raw["type"])
        session = object_json(raw.get("session") or {})
        native_session = raw.get("session_id") or session.get("id")
        if native_session != self.session_id:
            raise ValueError("event belongs to another session")
        id_ = text(raw["event_id"])
        if id_ in self._seen:
            return None
        turn = object_json(raw.get("turn") or {})
        if turn and turn.get("session_id") != self.session_id:
            raise ValueError("turn belongs to another session")
        turn_id_value = turn.get("id") or raw.get("turn_id")
        turn_id = text(turn_id_value) if turn_id_value is not None else None
        actions: tuple[RequiredAction, ...] = ()
        if kind == "agent.session.requires_action":
            actions = required_actions(session)
            action_turn = required_action_turn(actions)
            if action_turn is not None:
                if turn_id is not None and turn_id != action_turn:
                    raise ValueError("required-action turn disagrees with event identity")
                turn_id = action_turn
        child = turn.get("subagent_id")
        if child is not None and turn_id is not None:
            self._children[turn_id] = text(child)
        thread_id = self._children.get(turn_id or "")
        authority = "reconciled" if reconciled else "record"
        event_type = "native.openai." + kind
        payload: Object = dict(raw)
        item_id: str | None = None
        if kind == "error":
            error = object_json(raw["error"])
            event_type = "session.error"
            payload = object_json(
                SessionErrorPayload(
                    category=error_category(error.get("code") or error.get("type")),
                    retry_status="terminal",
                ).model_dump(mode="json")
            )
        elif thread_id is not None:
            event_type = "agent.thread.openai." + kind
        elif kind in (
            "agent.session.turn.completed",
            "agent.session.turn.failed",
            "agent.session.turn.cancelled",
        ):
            # Absence of subagent_id is ambiguous, not proof of a root turn.
            if "subagent_id" not in turn or turn_id is None:
                raise ValueError("terminal event lacks root identity")
            outcome = {"completed": "completed", "failed": "errored", "cancelled": "interrupted"}[
                kind.rsplit(".", 1)[1]
            ]
            previous = self._terminals.get(turn_id)
            if previous is not None:
                if previous != outcome:
                    raise ValueError("conflicting root outcomes")
                if previous == outcome:
                    return None
            self._terminals[turn_id] = outcome
            event_type = "session.turn_ended"
            payload = object_json(
                TurnEndedPayload.model_validate(
                    {"root_turn_id": turn_id, "outcome": outcome, "native_reason": kind}
                ).model_dump(mode="json")
            )
        elif kind == "agent.session.turn.in_progress":
            if "subagent_id" not in turn or turn_id is None:
                raise ValueError("running event lacks root identity")
            # Recovery starts buffering before the snapshot. A saved terminal
            # can therefore precede an older buffered running notification.
            if turn_id in self._terminals:
                return None
            event_type = "session.status_running"
            payload = object_json(
                StatusRunningPayload(root_turn_id=turn_id).model_dump(mode="json")
            )
        elif kind == "agent.session.requires_action":
            if turn_id is not None and self.terminal(turn_id):
                return None
            event_type = "session.requires_action"
            payload = object_json(RequiresActionPayload(actions=actions).model_dump(mode="json"))
        elif kind in ("agent.session.failed", "agent.session.environment.failed"):
            event_type = "session.status_terminated"
            payload = object_json(StatusTerminatedPayload(reason=kind).model_dump(mode="json"))
        elif kind == "agent.session.turn.output_text.delta":
            item_id = text(raw["item_id"])
            if item_id in self._final_items:
                return None
            event_type, authority = "agent.message.delta", "preview"
            payload = object_json(
                AgentMessageDeltaPayload.model_validate(
                    {
                        "item_id": item_id,
                        "content_index": raw["content_index"],
                        "text": raw["delta"],
                        "preview_sequence": self.sequence,
                    }
                ).model_dump(mode="json")
            )
        elif kind in ("agent.session.turn.item.done", "agent.session.turn.item.added"):
            item = object_json(raw["item"])
            item_id = text(item["id"])
            complete = item.get("status") in ("completed", "incomplete", "failed")
            if item_id in self._final_items:
                return None
            if complete:
                self._final_items.add(item_id)
            else:
                authority = "preview"
            item_kind = text(item["type"])
            if item_kind == "message":
                if item["role"] == "user":
                    event_type = "user.message"
                    payload = {
                        "input_id": item_id,
                        "content": [p.model_dump(mode="json") for p in parts(item["content"])],
                    }
                else:
                    event_type = "agent.message"
                    payload = object_json(
                        AgentMessagePayload(
                            item_id=item_id,
                            content=parts(item["content"]),
                            complete=complete,
                            phase=text(item["phase"]) if item.get("phase") is not None else None,
                        ).model_dump(mode="json")
                    )
            elif item_kind == "function_call":
                event_type = "agent.tool_use"
                payload = object_json(
                    ToolUsePayload(
                        call_id=text(item["call_id"]),
                        tool_name=text(item["name"]),
                        input=object_json(item["arguments"]),
                        executor="host",
                    ).model_dump(mode="json")
                )
            elif item_kind == "function_call_output":
                output = item.get("output")
                output_parts = (
                    (TextPart(text=output),) if isinstance(output, str) else parts(output or [])
                )
                event_type = "agent.tool_result"
                payload = object_json(
                    ToolResultPayload(
                        call_id=text(item["call_id"]),
                        content=output_parts,
                        is_error=item.get("error") is not None,
                    ).model_dump(mode="json")
                )
        elif kind.endswith(".delta") or kind == "agent.session.turn.output_text.done":
            authority = "preview"
        self._seen.add(id_)
        result = Event(
            id=id_,
            session_id=self.session_id,
            sequence=self.sequence,
            type=event_type,
            turn_id=turn_id,
            thread_id=thread_id,
            item_id=item_id,
            observed_at=datetime.now(UTC),
            authority=authority,
            payload=payload,
            native=NativeProvenance(
                provider="openai",
                api_revision=SCHEMA_DATE,
                event_id=id_,
                event_type=kind,
                ordering_domain=self.session_id,
            ),
        )
        self.sequence += 1
        return result

    def saved_item(self, item: Object) -> Event | None:
        return self.normalize(
            {
                "type": "agent.session.turn.item.done",
                "event_id": "openai:item:" + text(item["id"]),
                "session_id": self.session_id,
                "turn_id": item.get("turn_id"),
                "item": item,
            },
            reconciled=True,
        )

    def saved_turn(self, turn: Object) -> Event | None:
        status = text(turn["status"])
        if status not in ("completed", "cancelled", "failed", "in_progress"):
            return None
        return self.normalize(
            {
                "type": "agent.session.turn." + status,
                "event_id": "openai:turn:" + text(turn["id"]) + ":" + status,
                "session_id": self.session_id,
                "turn": turn,
            },
            reconciled=True,
        )

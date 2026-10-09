"""Pure translation of Managed Agents records and best-effort previews.

A normalizer is local to a chronological walk. Anthropic has no root-turn ID;
the host can supply its operation identity, otherwise the first running record
anchors it. Never infer an interruption from an interrupt input or its receipt.
"""

from collections.abc import Mapping
from datetime import datetime

from pydantic import JsonValue, TypeAdapter

from mux.contracts.events import Authority, Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.drivers.anthropic.usage import observation_from_event

API_REVISION = "managed-agents-2026-04-01"
_JSON = TypeAdapter(dict[str, JsonValue])


def object_json(value: object) -> dict[str, JsonValue]:
    return _JSON.validate_python(value)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("expected a native JSON object")
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a native string")
    return value


def _content(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError("expected native content parts")
    parts: list[JsonValue] = []
    for part in value:
        obj = _object(part)
        if obj.get("type") == "text":
            parts.append({"type": "text", "text": _text(obj["text"])})
        elif (
            obj.get("type") == "image"
            and isinstance(obj.get("source"), dict)
            and _object(obj["source"]).get("type") == "base64"
        ):
            source = _object(obj["source"])
            parts.append(
                {
                    "type": "image",
                    "media_type": _text(source["media_type"]),
                    "data_base64": _text(source["data"]),
                }
            )
        else:
            # Preserve content not represented by a neutral part without pretending
            # a provider file URL is a neutral, authorized artifact reference.
            parts.append(
                {"type": "native", "namespace": "anthropic.content", "version": 1, "payload": obj}
            )
    return parts


class EventNormalizer:
    """Translate one ordered source, remembering pending calls and turn identity.

    State is neither durable history nor accounting. Replays should start at
    the beginning or receive the host's root identity. Unknown action references
    stay native; missing history is never guessed to be a confirmation.
    """

    def __init__(self, session: ResourceRef, *, root_turn_id: str | None = None) -> None:
        self.session = session
        self.root_turn_id = root_turn_id
        self._host_root_turn_id = root_turn_id
        self.sequence = 0
        self._calls: dict[str, dict[str, JsonValue]] = {}
        self._preview_sequence: dict[str, int] = {}

    def normalize(self, raw: Mapping[str, JsonValue], *, observed_at: datetime) -> Event:
        data = object_json(raw)
        native_type = _text(data["type"])
        native_id = (
            data.get("event_id")
            if native_type == "event_delta"
            else _object(data["event"])["id"]
            if native_type == "event_start"
            else data["id"]
        )
        event_id = _text(native_id)
        thread = data.get("session_thread_id")
        thread_id = _text(thread) if thread is not None else None
        authority: Authority = "record"
        name = native_type
        item: str | None = None
        causes: tuple[str, ...] = ()
        payload: dict[str, JsonValue]
        if native_type in {"event_start", "event_delta"}:
            authority = "preview"
            if native_type == "event_delta":
                delta = _object(data["delta"])
                item = event_id
                preview_sequence = self._preview_sequence.get(item, 0)
                self._preview_sequence[item] = preview_sequence + 1
                name = "agent.message.delta"
                payload = {
                    "item_id": item,
                    "content_index": delta.get("index") or 0,
                    "text": _object(delta["content"])["text"],
                    "preview_sequence": preview_sequence,
                }
                event_id = f"{item}:preview:{preview_sequence}"
            else:
                name, payload = "native.event_start", data
                event_id = f"{event_id}:preview:start"
        elif native_type == "user.message":
            self.root_turn_id = self._host_root_turn_id or event_id
            payload = {"input_id": event_id, "content": _content(data["content"])}
        elif native_type == "agent.message":
            item = event_id
            payload = {"item_id": item, "content": _content(data["content"])}
        elif native_type in {"agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"}:
            self._calls[event_id] = data
            name, item = "agent.tool_use", event_id
            executor = {
                "agent.tool_use": "agent",
                "agent.mcp_tool_use": "mcp",
                "agent.custom_tool_use": "host",
            }[native_type]
            payload = {
                "call_id": event_id,
                "tool_name": data["name"],
                "input": data["input"],
                "executor": executor,
                "permission": "ask" if data.get("evaluated_permission") == "ask" else "auto",
                "mcp_server": data.get("mcp_server_name"),
            }
        elif native_type in {
            "agent.tool_result",
            "agent.mcp_tool_result",
            "user.custom_tool_result",
            "user.tool_result",
        }:
            pairing = {
                "agent.tool_result": "tool_use_id",
                "agent.mcp_tool_result": "mcp_tool_use_id",
                "user.custom_tool_result": "custom_tool_use_id",
                "user.tool_result": "tool_use_id",
            }[native_type]
            call_id = _text(data[pairing])
            name, item, causes = "agent.tool_result", event_id, (call_id,)
            payload = {
                "call_id": call_id,
                "content": _content(data["content"]),
                "is_error": bool(data.get("is_error", False)),
            }
        elif native_type == "session.status_running":
            self.root_turn_id = self.root_turn_id or event_id
            payload = {"root_turn_id": self.root_turn_id}
        elif native_type == "session.status_idle":
            stop = _object(data["stop_reason"])
            reason = _text(stop["type"])
            if reason == "requires_action":
                name = "session.requires_action"
                ids = stop["event_ids"]
                if not isinstance(ids, list):
                    raise ValueError("required action IDs must be a list")
                actions: list[JsonValue] = []
                for value in ids:
                    call_id = _text(value)
                    call = self._calls.get(call_id)
                    kind = (
                        "native"
                        if call is None
                        else (
                            "function_result"
                            if call["type"] == "agent.custom_tool_use"
                            else "tool_confirmation"
                        )
                    )
                    actions.append(
                        {
                            "id": call_id,
                            "call_id": call_id,
                            "kind": kind,
                            "native_type": None if call is None else call["type"],
                            "payload": {} if call is None else call,
                        }
                    )
                payload = {"actions": actions}
            elif reason in {"end_turn", "retries_exhausted"}:
                name = "session.turn_ended"
                payload = {
                    "root_turn_id": self.root_turn_id or event_id,
                    "outcome": "completed" if reason == "end_turn" else "errored",
                    "native_reason": reason,
                }
            else:
                name, payload = "native.session.status_idle", data
        elif native_type == "session.error":
            error = _object(data["error"])
            code = _text(error["type"])
            retry = _object(error["retry_status"])["type"]
            if (
                code in {"mcp_connection_failed_error", "mcp_authentication_failed_error"}
                and retry != "terminal"
            ):
                name = "tool_server.degraded"
                payload = {
                    "server": error["mcp_server_name"],
                    "error_type": code,
                    "retry_status": retry,
                }
            else:
                category = {
                    "model_rate_limited_error": "rate_limited",
                    "model_overloaded_error": "overloaded",
                }.get(code, "upstream")
                payload = {
                    "category": category,
                    "retry_status": retry,
                    "native_code": code,
                    "message": error.get("message"),
                }
        elif native_type == "session.status_terminated":
            payload = {"reason": "session terminated by MA", "native_lifecycle": "terminated"}
        elif native_type == "span.model_request_end":
            name = "usage.observed"
            observation = observation_from_event(
                data,
                self.session,
                observed_at=observed_at,
                turn_id=self.root_turn_id,
                thread_id=thread_id,
            )
            payload = {"observation_id": observation.id, "revision": observation.revision}
            causes = (_text(data["model_request_start_id"]),)
        elif native_type.startswith("session.thread_"):
            name = "agent.thread." + native_type.removeprefix("session.thread_")
            payload = data
        else:
            name = (
                native_type if native_type.startswith("agent.thread.") else f"native.{native_type}"
            )
            payload = data
        occurred = data.get("processed_at")
        occurred_at = (
            datetime.fromisoformat(occurred.replace("Z", "+00:00"))
            if isinstance(occurred, str)
            else None
        )
        event = Event(
            id=event_id,
            session_id=self.session.id,
            sequence=self.sequence,
            type=name,
            turn_id=self.root_turn_id,
            thread_id=thread_id,
            item_id=item,
            caused_by=causes,
            observed_at=observed_at,
            occurred_at=occurred_at,
            authority=authority,
            payload=payload,
            native=NativeProvenance(
                provider="anthropic",
                api_revision=API_REVISION,
                event_id=_text(native_id),
                event_type=native_type,
                cursor=_text(native_id) if authority == "record" else None,
                ordering_domain=self.session.id,
                record=data,
            ),
        )
        self.sequence += 1
        if name in {"session.turn_ended", "session.status_terminated"}:
            self.root_turn_id = self._host_root_turn_id
            self._calls.clear()
        return event

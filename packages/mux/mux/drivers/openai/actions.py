"""Input codecs verified against Agents events.create (2026-10-09)."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import JsonValue

from mux.contracts.actions import InputEvent, UserMessage, UserToolResult
from mux.contracts.events import ContentPart, ImagePart, TextPart
from mux.drivers.openai._common import objects, text
from mux.drivers.openai.transport import Object
from mux.errors import UnsupportedCapability


def content(parts: Sequence[ContentPart], profile_id: str) -> list[JsonValue]:
    result: list[JsonValue] = []
    for part in parts:
        if isinstance(part, TextPart):
            result.append({"type": "input_text", "text": part.text})
        elif isinstance(part, ImagePart) and part.data_base64 is not None:
            result.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{part.media_type};base64,{part.data_base64}",
                }
            )
        else:
            raise UnsupportedCapability(("input_content",), profile_id)
    return result


def translate(events: Sequence[InputEvent], session: Object, profile_id: str) -> list[JsonValue]:
    result: list[JsonValue] = []
    actions = objects(session.get("required_actions", []))
    for event in events:
        if isinstance(event, UserMessage):
            running = session.get("status") in ("in_progress", "requires_action")
            if (event.mode == "steer") != running:
                # OpenAI decides new-turn vs steer from native occupancy. Do not
                # silently change a caller's requested mode.
                raise UnsupportedCapability(("input_mode_precondition",), profile_id)
            result.append(
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": content(event.content, profile_id)}],
                }
            )
        elif isinstance(event, UserToolResult):
            action = next(
                (
                    a
                    for a in actions
                    if a.get("call_id") == event.action_id and a.get("type") == "function_call"
                ),
                None,
            )
            if action is None:
                raise UnsupportedCapability(("function_result_action",), profile_id)
            wire: Object = {
                "type": "agent.session.input.tool_result",
                "call_id": event.action_id,
                "turn_id": text(action["turn_id"]),
                "success": not event.is_error,
            }
            if event.is_error:
                if any(not isinstance(p, TextPart) for p in event.content):
                    raise UnsupportedCapability(("function_error_content",), profile_id)
                wire["error"] = "\n".join(p.text for p in event.content if isinstance(p, TextPart))
            else:
                wire["output"] = content(event.content, profile_id)
            result.append(wire)
        else:
            # A computer-use approval is not a generic tool confirmation.
            raise UnsupportedCapability(
                ("native_input" if event.type == "native.input" else "tool_confirmation",),
                profile_id,
            )
    return result

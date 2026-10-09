"""Input translation. Native input is deliberately closed until a typed port lands."""

from collections.abc import Sequence
from typing import cast

from anthropic.types.beta.sessions.beta_managed_agents_event_params import (
    BetaManagedAgentsEventParams,
)
from pydantic import JsonValue

from mux.contracts.actions import (
    InputEvent,
    UserMessage,
    UserToolConfirmation,
    UserToolResult,
)
from mux.contracts.events import ContentPart, ImagePart, NativePart, TextPart
from mux.errors import UnsupportedCapability


def _content(parts: tuple[ContentPart, ...]) -> list[JsonValue]:
    content: list[JsonValue] = []
    for part in parts:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart) and part.data_base64 is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.media_type,
                        "data": part.data_base64,
                    },
                }
            )
        elif isinstance(part, NativePart):
            raise UnsupportedCapability((part.namespace,), "anthropic.managed_agents")
        else:
            raise UnsupportedCapability(("artifact_input",), "anthropic.managed_agents")
    return content


def translate_inputs(events: Sequence[InputEvent]) -> list[BetaManagedAgentsEventParams]:
    translated: list[dict[str, JsonValue]] = []
    for event in events:
        if isinstance(event, UserMessage):
            if event.mode == "steer":
                raise UnsupportedCapability(("steer",), "anthropic.managed_agents")
            translated.append({"type": "user.message", "content": _content(event.content)})
        elif isinstance(event, UserToolConfirmation):
            confirmation: dict[str, JsonValue] = {
                "type": "user.tool_confirmation",
                "tool_use_id": event.action_id,
                "result": event.decision,
            }
            if event.deny_message is not None:
                if event.decision != "deny":
                    raise ValueError("deny_message requires a deny decision")
                confirmation["deny_message"] = event.deny_message
            translated.append(confirmation)
        elif isinstance(event, UserToolResult):
            translated.append(
                {
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": event.action_id,
                    "content": _content(event.content),
                    "is_error": event.is_error,
                }
            )
        else:
            raise UnsupportedCapability((event.extension.namespace,), "anthropic.managed_agents")
    return cast(list[BetaManagedAgentsEventParams], translated)

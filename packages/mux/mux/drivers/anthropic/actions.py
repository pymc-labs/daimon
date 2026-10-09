"""Neutral input translation and the closed privileged system-message extension."""

from collections.abc import Sequence
from typing import cast

from anthropic.types.beta.sessions.beta_managed_agents_event_params import (
    BetaManagedAgentsEventParams,
)
from pydantic import JsonValue

from mux.contracts._base import Contract
from mux.contracts.actions import (
    InputEvent,
    NativeInput,
    UserMessage,
    UserToolConfirmation,
    UserToolResult,
)
from mux.contracts.events import ContentPart, ImagePart, NativePart, TextPart
from mux.errors import ExtensionVersionError, UnsupportedCapability


class SystemMessageConfig(Contract):
    """Anthropic-only privileged text framing; no arbitrary event or SDK kwargs."""

    content: tuple[TextPart, ...]


def _system_message(event: NativeInput) -> dict[str, JsonValue]:
    extension = event.extension
    if extension.namespace != "anthropic.session_system_message":
        raise UnsupportedCapability((extension.namespace,), "anthropic.managed_agents")
    if extension.version != 1:
        raise ExtensionVersionError(extension.namespace, extension.version, (1,))
    config = SystemMessageConfig.model_validate(extension.value)
    return {
        "type": "system.message",
        "content": [part.model_dump(mode="json") for part in config.content],
    }


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
    for index, event in enumerate(events):
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
            native = _system_message(event)
            # The API requires exactly one privileged event at the end,
            # immediately after the input it frames. Validate the whole batch
            # before the caller starts provider I/O.
            if (
                not translated
                or translated[-1]["type"] not in {"user.message", "user.custom_tool_result"}
                or index != len(events) - 1
            ):
                raise ValueError("system.message must be final and immediately follow user input")
            translated.append(native)
    return cast(list[BetaManagedAgentsEventParams], translated)

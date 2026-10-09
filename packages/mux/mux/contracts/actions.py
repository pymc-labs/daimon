"""Input sent into a session. `RequiredAction` lives in `events` and is re-exported here."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from mux.contracts._base import Tagged
from mux.contracts.events import ContentPart, RequiredAction
from mux.contracts.extensions import ExtensionConfig


class UserMessage(Tagged):
    type: Literal["user.message"] = "user.message"
    content: tuple[ContentPart, ...]
    mode: Literal["new_turn", "steer"] = "new_turn"


class UserToolConfirmation(Tagged):
    """An answer to a tool-permission prompt. Not interchangeable with a tool result."""

    type: Literal["user.tool_confirmation"] = "user.tool_confirmation"
    action_id: str
    decision: Literal["allow", "deny"]
    deny_message: str | None = None


class UserToolResult(Tagged):
    type: Literal["user.tool_result"] = "user.tool_result"
    action_id: str
    content: tuple[ContentPart, ...]
    is_error: bool = False


class NativeInput(Tagged):
    """Provider-native input, gated by the extension it names before sending."""

    type: Literal["native.input"] = "native.input"
    extension: ExtensionConfig


InputEvent = Annotated[
    UserMessage | UserToolConfirmation | UserToolResult | NativeInput,
    Field(discriminator="type"),
]


__all__ = [
    "InputEvent",
    "NativeInput",
    "RequiredAction",
    "UserMessage",
    "UserToolConfirmation",
    "UserToolResult",
]

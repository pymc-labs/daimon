"""Reaction actions: a custom emoji that marks a message, and one that confirms it.

A binding ties one attached server, one tool and one emoji to each of two steps.
The mark is added by the chat adapter after a turn in which the agent's call of
`mark_tool` finished without error. A person who adds the confirm emoji to a
marked message makes the adapter call `confirm_tool` for them, with no model turn.

This module is pure: the binding model, the match rules and the result
convention. The adapters do the I/O. Removing the confirm emoji does nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from daimon.core.turn.state import ContentBlock, ToolUseBlock

#: Where the MCP service takes the call, below the app root.
CALL_PATH: Final = "/internal/reaction-actions/call"
#: The warning emoji when a binding names none.
DEFAULT_WARNING: Final = "\N{WARNING SIGN}\N{VARIATION SELECTOR-16}"
#: Longest tool text kept: it goes into one short card.
MAX_TEXT: Final = 300
#: Shown when the tool gave no text of its own.
NO_TEXT: Final = "The action did not go through."

_EMOJI: Final = re.compile(r"[A-Za-z0-9_]{2,32}:(\d{1,20})")

Status = Literal["ok", "attention", "error"]


class ReactionAction(BaseModel):
    """One binding. `workspace` is `<platform>:<workspace id>`; `agent` is the agent's name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: str = Field(pattern=r"^[a-z]+:[0-9A-Za-z_-]+$")
    server: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    agent: str = Field(min_length=1)
    mark_tool: str = Field(min_length=1)
    mark_emoji: str
    confirm_tool: str = Field(min_length=1)
    confirm_emoji: str
    warning_emoji: str = DEFAULT_WARNING

    @field_validator("mark_emoji", "confirm_emoji")
    @classmethod
    def _custom_emoji(cls, value: str) -> str:
        if not _EMOJI.fullmatch(value):
            raise ValueError('a custom emoji is written "name:id"')
        return value

    @field_validator("warning_emoji")
    @classmethod
    def _unicode_emoji(cls, value: str) -> str:
        if not value or ":" in value or value.isascii():
            raise ValueError("the warning emoji is a unicode emoji")
        return value

    @model_validator(mode="after")
    def _distinct(self) -> ReactionAction:
        if self.mark_emoji_id == self.confirm_emoji_id:
            raise ValueError("the mark and confirm emoji must differ")
        return self

    @property
    def mark_emoji_id(self) -> int:
        return emoji_id(self.mark_emoji)

    @property
    def confirm_emoji_id(self) -> int:
        return emoji_id(self.confirm_emoji)


def emoji_id(emoji: str) -> int:
    match = _EMOJI.fullmatch(emoji)
    if match is None:
        raise ValueError("not a custom emoji")
    return int(match.group(1))


def _role_emoji(action: ReactionAction) -> tuple[tuple[str, str], ...]:
    return (
        ("mark", str(action.mark_emoji_id)),
        ("confirm", str(action.confirm_emoji_id)),
        ("warning", action.warning_emoji),
    )


def overlap_error(actions: Iterable[ReactionAction]) -> str | None:
    """One sentence naming two bindings in a workspace that share an emoji, else None.

    The listener picks a binding by workspace and confirm emoji id, and the mark hook
    by workspace, so the workspace (one Discord server) is the scope. Two bindings may
    share a warning emoji, since it grants nothing; any other shared emoji lets one
    binding's confirm add another's mark, or picks a binding by order.
    """
    seen: dict[tuple[str, str], tuple[ReactionAction, str]] = {}
    for action in actions:
        for role, key in _role_emoji(action):
            earlier = seen.get((action.workspace, key))
            if earlier is None:
                seen[(action.workspace, key)] = (action, role)
            elif earlier[0] is action or role != "warning" or earlier[1] != "warning":
                first = earlier[0]
                return (
                    f"reaction actions {_label(first)} ({earlier[1]}) and {_label(action)} "
                    f"({role}) in {action.workspace} both use the emoji {key}, "
                    "so each emoji must be used once"
                )
    return None


def _label(action: ReactionAction) -> str:
    return f'"{action.server}/{action.agent}/{action.mark_tool}"'


def bindings_for(actions: Iterable[ReactionAction], workspace: str) -> tuple[ReactionAction, ...]:
    return tuple(action for action in actions if action.workspace == workspace)


def confirm_binding(
    actions: Iterable[ReactionAction], *, workspace: str, emoji_id_: int
) -> ReactionAction | None:
    """The binding whose confirm emoji is `emoji_id_` in `workspace`, if any."""
    for action in actions:
        if action.workspace == workspace and action.confirm_emoji_id == emoji_id_:
            return action
    return None


def message_url(guild_id: str, channel_id: str, message_id: str) -> str:
    """The link of one message, as the platform shows it."""
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


@dataclass(frozen=True, slots=True)
class Outcome:
    status: Status
    text: str = ""


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return (
        text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"
    )


def _object(text: str) -> Mapping[str, object] | None:
    try:
        parsed: object = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return cast("Mapping[str, object]", parsed) if isinstance(parsed, dict) else None


def _texts(content: object) -> list[str]:
    texts: list[str] = []
    for item in cast("list[object]", content) if isinstance(content, list) else ():
        text = cast("dict[str, object]", item).get("text") if isinstance(item, dict) else None
        if isinstance(text, str):
            texts.append(text)
    return texts


def outcome_of(result: Mapping[str, object]) -> Outcome:
    """What a `tools/call` result means for a reaction action.

    `isError` true is an error. An object with `"status": "attention"` (as
    `structuredContent`, or as the one text item parsed as JSON) needs attention,
    and its `"message"` is the text. Anything else is ok.
    """
    texts = _texts(result.get("content"))
    if result.get("isError") is True:
        return Outcome("error", _clip(" ".join(texts)) or NO_TEXT)
    structured = result.get("structuredContent")
    body = cast("Mapping[str, object]", structured) if isinstance(structured, dict) else None
    if body is None and len(texts) == 1:
        body = _object(texts[0])
    if body is not None and body.get("status") == "attention":
        message = body.get("message")
        return Outcome("attention", _clip(message) if isinstance(message, str) else NO_TEXT)
    return Outcome("ok")


def blocks_text(block: ToolUseBlock) -> Mapping[str, object]:
    """A tool block's result in the shape `outcome_of` reads."""
    texts: list[object] = []
    for item in block.result_content or ():
        text = getattr(item, "text", None)
        if isinstance(text, str):
            texts.append({"type": "text", "text": text})
    return {"isError": block.is_error, "content": texts}


def marking_actions(
    content: Sequence[ContentBlock],
    actions: Iterable[ReactionAction],
    *,
    agent_name: str,
    server_urls: Mapping[str, str],
    public_url: str | None,
) -> tuple[ReactionAction, ...]:
    """The bindings whose mark tool finished without error or attention in a turn.

    A tool block carries only the label the agent lists its server under, which any
    agent may choose. So a block counts only when the executing agent is the binding's
    agent, and `server_urls` (that agent's own list, label to URL) maps the block's
    label to this deployment's relay URL for the binding's server. One mark per binding.
    """
    from daimon.core.mcp_identity import relay_server_name
    from daimon.core.turn.state import ToolUseBlock

    if public_url is None:
        return ()
    found: list[ReactionAction] = []
    for action in actions:
        if action.agent != agent_name:
            continue
        for block in content:
            url = (
                server_urls.get(block.mcp_server_name or "")
                if isinstance(block, ToolUseBlock)
                else None
            )
            if (
                isinstance(block, ToolUseBlock)
                and url is not None
                and relay_server_name(url, public_url) == action.server
                and block.name == action.mark_tool
                and block.status == "complete"
                and not block.is_error
                and outcome_of(blocks_text(block)).status == "ok"
            ):
                found.append(action)
                break
    return tuple(found)

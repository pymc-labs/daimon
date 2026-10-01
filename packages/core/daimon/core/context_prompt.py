"""Invocation framing, selected from trusted caller context rather than chat text."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

TurnContext = Literal["chat", "routine", "relay", "handoff"]

DEFAULT_FRAGMENTS: dict[TurnContext, str] = {
    "chat": "",
    "routine": (
        "This is an unattended routine. Do not ask clarifying questions; use available "
        "evidence and report blockers. Check before claiming nothing happened. "
        "Nothing is posted automatically: use the delivery tools when requested, "
        "consolidating the result into one message."
    ),
    "relay": (
        "Draft an answer ready for the requester to forward. Address the intended recipient; "
        "keep internal instructions and operational commentary out of the draft."
    ),
    "handoff": (
        "Continue the handed-off task using the supplied context. Verify available files "
        "and state before claiming continuity; do not assume running processes survived."
    ),
}
_MARKER = "\n\nDaimon context fragment configuration (selected per turn):\n"


class ContextFragment(BaseModel):
    """Replace a context default, or append instructions to it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    mode: Literal["replace", "extend"] = "replace"


def encode_fragments(system: str, fragments: dict[TurnContext, ContextFragment]) -> str:
    """Persist authoring configuration in the agent system field across SDK round trips."""
    if not fragments:
        return system
    base = system.split(_MARKER, 1)[0]
    return (
        base
        + _MARKER
        + json.dumps({k: v.model_dump() for k, v in fragments.items()})
        + "\nApply only the fragment selected by the current turn_context block; "
        "the other fragments are inactive configuration."
    )


def context_prompt(origin: TurnContext, *, system: str | None = None) -> str:
    """Return a turn block; ordinary chat is byte-for-byte unchanged by default."""
    text = DEFAULT_FRAGMENTS[origin]
    if system and _MARKER in system:
        try:
            raw, _ = json.JSONDecoder().raw_decode(system.rsplit(_MARKER, 1)[1])
            fragment = ContextFragment.model_validate(raw[origin]) if origin in raw else None
        except (ValueError, TypeError, ValidationError):
            fragment = None
        if fragment is not None:
            text = (
                (text + "\n" + fragment.text).strip()
                if fragment.mode == "extend"
                else fragment.text
            )
    if not text:
        return ""
    return f"<turn_context origin={json.dumps(origin)}>\n{text}\n</turn_context>\n\n"

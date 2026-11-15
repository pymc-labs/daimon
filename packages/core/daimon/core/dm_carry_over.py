"""An earlier private conversation, imported by an operator for a person's first DM here."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal
from xml.sax.saxutils import escape

from daimon.core.handoff_context import render_previous_session, select_recent_turn_texts
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

MAX_EXPORT_BYTES = 1048576
#: What the first reply carries in full; later turns keep the usual rolling window.
HISTORY_TURNS = 12
HISTORY_CHARS = 16_000
#: Older turns, kept as fixed framing for the whole conversation.
CONTEXT_TURNS = 100
CONTEXT_CHARS = 8_000
MAX_SOURCE_NAME = 64

_UNSAFE = re.compile(r"[\x00-\x1f\x7f  ]+")

DiscordId = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,20}$")]


class CarryOverTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    text: str
    at: datetime


class CarryOverUser(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_user_id: DiscordId
    turns: list[CarryOverTurn]


class CarryOverExport(BaseModel):
    """v1 file: each listed person's earlier conversation with the assistant they used."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    guild_id: DiscordId
    source_name: str
    exported_at: datetime
    users: list[CarryOverUser]

    @field_validator("source_name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        name = _UNSAFE.sub(" ", value).strip()[:MAX_SOURCE_NAME].strip()
        if not name:
            raise ValueError("source_name is empty")
        return name


class BuiltCarryOver(BaseModel):
    model_config = ConfigDict(frozen=True)

    history: list[dict[str, str]]
    context: str


def build_carry_over(
    turns: Sequence[CarryOverTurn], *, source_name: str, exported_at: datetime
) -> BuiltCarryOver:
    """The latest turns as history, the older ones as quoted context.

    The assistant's side maps to the agent's. Blank turns are dropped, and every
    text is redacted like any other earlier conversation.
    """
    ordered = sorted(turns, key=lambda turn: turn.at)
    texts: list[tuple[Literal["user", "agent"], str]] = [
        ("user" if turn.role == "user" else "agent", turn.text.strip())
        for turn in ordered
        if turn.text.strip()
    ]
    latest = select_recent_turn_texts(texts, max_turns=HISTORY_TURNS, max_chars=HISTORY_CHARS)
    older = select_recent_turn_texts(
        texts[: len(texts) - len(latest)], max_turns=CONTEXT_TURNS, max_chars=CONTEXT_CHARS
    )
    header = f"Earlier conversation with {escape(source_name)}, exported {exported_at:%Y-%m-%d}."
    context = (
        header + "\n" + render_previous_session(older, from_agent_name=source_name)
        if older
        else header
    )
    return BuiltCarryOver(
        history=[{"role": turn.role, "text": turn.text} for turn in latest], context=context
    )

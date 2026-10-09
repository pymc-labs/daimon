"""Ask the person who started a turn to approve one action, and wait.

The hook every chat adapter implements so core can pause for a human: a
`ConfirmationPrompt` goes in (what will happen, who may answer, until when),
a `ConfirmationAnswer` comes out. Core builds prompts for gated tool writes
(`prompt_for_tool_call`); a plugin that wants its own review step builds its
own prompt and calls the same hook, so it gets the same card on every
platform for free.

The safe default is `no_confirmation_surface`: an adapter that has no way to
show a card (the CLI today) answers `denied`, so a gated write is
refused with a plain message rather than run unseen.

`PendingConfirmations` is the in-process rendezvous an adapter uses between
posting a card and its button click: `open` hands out a token and a future,
the click handler `resolve`s the token. A process restart loses the pending
futures, which is fine — the turn waiting on them died with the process.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, cast

from daimon.core.tool_safety import DAIMON_SERVER_NAME, PUBLISH_TOOLS, ToolCall
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "CONFIRMATION_TIMEOUT",
    "ConfirmationAnswer",
    "ApprovedConfirmation",
    "ConfirmationHook",
    "ConfirmationPrompt",
    "PendingConfirmations",
    "no_confirmation_surface",
    "prompt_for_tool_call",
]

ConfirmationAnswer = Literal["approved", "denied", "expired"]

#: How long a card waits for its button before the action is refused. The
#: session sits idle meanwhile (no model spend), and the per-turn ceiling
#: still bounds the whole turn.
CONFIRMATION_TIMEOUT: Final[timedelta] = timedelta(minutes=10)


class ConfirmationPrompt(BaseModel):
    """What a confirmation card shows, platform-neutral.

    `detail_lines` shows short, readable inputs behind Details. Only
    `requester_platform_user_id` may answer.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    consequence: str | None = None
    action: str | None = None
    denied_action: str | None = None
    detail_lines: tuple[str, ...] = ()
    requester_platform_user_id: str
    requester_display_name: str | None = None
    expires_at: datetime

    @model_validator(mode="after")
    def _aware_expiry(self) -> ConfirmationPrompt:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return self


@dataclass(frozen=True)
class ApprovedConfirmation:
    """An approved card that can be retired if its allow is never sent."""

    answer: Literal["approved"]
    retire_unsent: Callable[[], Awaitable[None]]


ConfirmationHook = Callable[
    [ConfirmationPrompt], Awaitable[ConfirmationAnswer | ApprovedConfirmation]
]


async def no_confirmation_surface(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
    """Default hook for a surface that cannot show a card: refuse."""
    del prompt
    return "denied"


def _short(value: object, *, limit: int = 80) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _generic_details(tool_input: dict[str, object]) -> tuple[str, ...]:
    lines: list[str] = []
    for key, value in tool_input.items():
        lower = key.lower()
        if (
            lower in {"origin_context_id", "server", "tool", "content_hash"}
            or lower == "id"
            or lower.endswith("_id")
            or any(word in lower for word in ("token", "secret", "password"))
            or value is None
            or not isinstance(value, str | int | float | bool)
            or (isinstance(value, str) and value.lstrip().startswith(("{", "[")))
            or (isinstance(value, str) and "://" in value and len(value) > 60)
        ):
            continue
        value_text = _short(str(value).replace("\n", " "))
        lines.append(f"{key.replace('_', ' ').capitalize()}: {value_text}")
        if len(lines) == 4:
            break
    return tuple(lines)


def _recipients(value: object) -> str:
    if not isinstance(value, list):
        return str(value or "recipients")
    names: list[str] = []
    for item in cast(list[object], value):
        if isinstance(item, dict):
            entry = cast(dict[str, object], item)
            names.append(str(entry.get("label") or entry.get("name") or "recipient"))
        else:
            names.append(str(item))
    return ", ".join(names)


def prompt_for_tool_call(
    call: ToolCall,
    *,
    requester_platform_user_id: str,
    now: datetime,
    timeout: timedelta = CONFIRMATION_TIMEOUT,
) -> ConfirmationPrompt:
    """The card for one gated write, with short product-facing details."""
    server = call.server_name or "a tool"
    publishing = call.tool_name in PUBLISH_TOOLS and server == DAIMON_SERVER_NAME
    data = call.input
    name = str(data.get("name") or "")
    slug = str(data.get("slug") or "")
    title_value = str(data.get("title") or "")
    notebook = f'notebook "{slug}"' if slug else "notebook"
    title: str
    action: str
    denied_action: str
    consequence: str | None = None
    detail_lines: tuple[str, ...]
    if call.tool_name == "create_notebook_upload_url" and publishing:
        action = f"Publish {notebook}"
        title = f"{action}?"
        denied_action = f"{notebook[0].upper()}{notebook[1:]} not published"
        consequence = (
            "Anyone with the link can open and edit it."
            if data.get("editable")
            else "Anyone with the link can open it."
        )
        days = data.get("ttl_days") or 1
        detail_lines = (
            f"Notebook: {slug or 'New notebook'}",
            f"Link expires: {'Never' if data.get('permanent') else f'in {days} days'}",
            f"Editable: {'Yes' if data.get('editable') else 'No'}",
        )
    elif call.tool_name == "create_attachment_upload_url" and publishing:
        action = f'Upload "{name}" to {notebook}'
        title = f"{action}?"
        denied_action = f'"{name}" not uploaded'
        consequence = "Anyone with the notebook's link can open this file."
        detail_lines = (f"File: {_short(name)}", f"Notebook: {_short(slug)}")
    elif call.tool_name == "publish_report" and publishing:
        action = f'Publish report "{title_value}"'
        title = f"{action}?"
        denied_action = f'Report "{title_value}" not published'
        recipients = _recipients(data.get("recipients"))
        consequence = f"Shared with {recipients}. Anyone with the link can open it."
        cap = data.get("cap_usd")
        detail_lines = (
            f"Report: {_short(title_value)}",
            f"Shared with: {_short(recipients)}",
            f"Spending cap: ${cap}" if cap is not None else "Spending cap: Not set",
        )
    elif call.tool_name == "add_skill" and server == DAIMON_SERVER_NAME:
        agent = str(data.get("agent_name") or data.get("agent") or "agent")
        if not name:
            source = str(data.get("path") or data.get("attachment_url") or "")
            name = source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".md") or "skill"
            skill_md = data.get("skill_md")
            if name == "skill" and isinstance(skill_md, str):
                match = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+)", skill_md)
                if match:
                    name = match.group(1).strip()
        action = f'Add skill "{name}" to {agent}'
        title = f"{action}?"
        denied_action = f'Skill "{name}" not added'
        consequence = f"This changes {agent} for everyone who uses it."
        source_label = (
            "attached file"
            if data.get("attachment_url")
            else "GitHub repo"
            if data.get("repo_url")
            else "pasted text"
        )
        detail_lines = (
            f"Skill: {_short(name)}",
            f"Agent: {_short(agent)}",
            f"Source: {source_label}",
        )
    else:
        verb = call.tool_name.replace("_", " ").capitalize()
        subject = title_value or name
        subject_label = f' "{subject}"' if subject else ""
        action = f"{verb}{subject_label}"
        title = f"{action}?"
        denied_action = f"{verb} not completed"
        detail_lines = _generic_details(data)
    return ConfirmationPrompt(
        title=title,
        consequence=consequence,
        action=action,
        denied_action=denied_action,
        detail_lines=detail_lines,
        requester_platform_user_id=requester_platform_user_id,
        expires_at=now + timeout,
    )


class PendingConfirmations:
    """Token → future map between a posted card and its click. No I/O."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ConfirmationAnswer]] = {}

    def open(self) -> tuple[str, asyncio.Future[ConfirmationAnswer]]:
        token = secrets.token_urlsafe(12)
        future: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
        self._pending[token] = future
        return token, future

    def resolve(self, token: str, answer: ConfirmationAnswer) -> bool:
        """Settle `token`. False when it is unknown or already answered."""
        future = self._pending.pop(token, None)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def discard(self, token: str) -> None:
        self._pending.pop(token, None)

    async def wait(
        self, token: str, future: asyncio.Future[ConfirmationAnswer], *, timeout_s: float
    ) -> ConfirmationAnswer:
        """Wait for the click; `expired` on timeout. Always forgets `token`."""
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        except TimeoutError:
            return "expired"
        finally:
            self.discard(token)

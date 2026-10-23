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
import json
import re
import secrets
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, cast
from urllib.parse import unquote

from daimon.core.fleet_configuration import (
    ConfigurationReference,
    complete_fleet_config,
    configuration_refusal,
    configured_fleet_grant,
    fleet_default_line,
    fleet_detail_lines,
    provider_preview_digest,
)
from daimon.core.microsoft_write_paths import write_kind
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    PUBLISH_TOOLS,
    ToolCall,
    microsoft_send_tools,
)
from daimon.core.tool_safety import backend_switch_tools as backend_switch_tools
from mux.contracts.config import ThreadMode
from mux.contracts.ids import ChannelRef, Provider
from mux.contracts.profile import Capability
from mux.contracts.resources import AgentSpec
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from daimon.core.channel_backend import AgentBackendService, AgentSettings
    from daimon.core.neutral_resources import NeutralResourceHost
    from daimon.core.stores.domain import BackendAgent, CompatibilityPreview
    from daimon.core.turn.deps import TurnDeps
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "CONFIRMATION_TIMEOUT",
    "SEND_BODY_LIMIT",
    "ConfirmationAnswer",
    "ApprovedConfirmation",
    "ConfirmationHook",
    "ConfirmationPrompt",
    "BackendSwitchReference",
    "ToolRuleCardReference",
    "PendingConfirmations",
    "no_confirmation_surface",
    "prompt_for_tool_call",
]

ConfirmationAnswer = Literal["approved", "denied", "expired"]

#: How long a card waits for its button before the action is refused. The
#: session sits idle meanwhile (no model spend), and the per-turn ceiling
#: still bounds the whole turn.
CONFIRMATION_TIMEOUT: Final[timedelta] = timedelta(minutes=10)

#: Longest `microsoft_send` body, so the whole of it fits behind Details. The
#: tool refuses a longer one.
SEND_BODY_LIMIT: Final[int] = 8000
#: How much of that body the card shows before Details.
_SEND_PREVIEW: Final[int] = 400
#: Longest `microsoft_write` path the tool accepts, so the card shows all of it.
WRITE_PATH_LIMIT: Final[int] = 500
#: Longest `microsoft_write` body in characters (a text body's, or a JSON body's compact form).
WRITE_BODY_LIMIT: Final[int] = 8000
#: Longest `microsoft_write` body in UTF-8 bytes. Emoji and most CJK text cost 3 to 4 bytes a
#: character on the card, and Teams refuses a message over about 28 KB, so a body of such text is
#: cut shorter than WRITE_BODY_LIMIT. Together the two let Details hold the whole body. The tool
#: refuses more than either.
WRITE_BODY_BYTES: Final[int] = 14000
#: Most UTF-8 bytes a `microsoft_send`'s addresses, subject, location, start, end and time zone
#: may total. The card shows each in three places and Teams refuses a message over about 28 KB,
#: so this keeps room for a full 8,000-character body. The tool refuses more.
SEND_HEADER_BYTES: Final[int] = 3000
#: Longest `microsoft_write` path the card shows before Details.
_WRITE_PATH_PREVIEW: Final[int] = 120


class BackendSwitchReference(BaseModel):
    """Untrusted tool reference; hosts reload the bound server-side preview."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str = Field(min_length=1)
    target_backend: Provider
    model: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    expected_revision: int = Field(ge=0, strict=True)
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted_drops: tuple[str, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # The agent's own settings, when the request states them: both together,
    # an unset one returning to the backend's default.
    thread_mode: ThreadMode | None = Field(default=None, exclude_if=lambda value: value is None)
    required_capabilities: tuple[Capability, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("accepted_drops", mode="before")
    @classmethod
    def _accepted_drops(cls, value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        return TypeAdapter(
            AgentSpec.model_fields["declared_drops"].rebuild_annotation()
        ).validate_python(value)


class ToolRuleCardReference(BaseModel):
    """The endpoint and rule-key revisions a posted approval card captured."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    agent_id: str
    agent_name: str
    tool_key: str
    server_url: str
    channel_id: str | None = None
    agent_revision: int = Field(default=0, ge=0)
    channel_revision: int = Field(default=0, ge=0)


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
    # Opt-in only: legacy prompts and their rendered cards stay unchanged.
    admin: bool = Field(default=False, exclude_if=lambda value: not value)
    backend_switch: BackendSwitchReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    configuration: ConfigurationReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    tool_rule: ToolRuleCardReference | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    tool_rules_enabled: bool = Field(default=False, exclude_if=lambda value: not value)
    # A publish that may leave a lasting public link (a report, any notebook
    # upload, a file in a notebook): never answered for a test bot.
    lasting_public_link: bool = Field(default=False, exclude_if=lambda value: not value)

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


def _in_days(days: object) -> str:
    return f"in {days} day" if days == 1 else f"in {days} days"


def _short(value: object, *, limit: int = 80) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plain(text: str) -> str:
    """`text` without control, invisible (such as direction override) or line-break characters."""
    return "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf", "Zl", "Zp"})


def is_plain(text: str) -> bool:
    """Whether `text` holds no control, invisible or line-break characters."""
    return _plain(text) == text


def _one_line(value: object, *, limit: int | None = None) -> str:
    """A model-supplied field as one plain line, so it can't start a line of the card.

    Every kind of whitespace, including each line break, becomes one space.
    """
    text = _plain(" ".join(str(value).split()))
    return text if limit is None else _short(text, limit=limit)


def _clip_middle(text: str, limit: int) -> str:
    """`text` cut to `limit` characters in the middle, so its start and its end both show."""
    if len(text) <= limit:
        return text
    head = (limit - 1) // 3
    return f"{text[:head]}…{text[len(text) - (limit - 1 - head) :]}"


def _preview(text: str) -> str:
    """The first `_SEND_PREVIEW` characters of one line, saying how much is left."""
    if len(text) <= _SEND_PREVIEW:
        return text
    return f"{text[:_SEND_PREVIEW]}… (+{len(text) - _SEND_PREVIEW} more characters, see Details)"


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
        return _one_line(value or "recipients")
    names: list[str] = []
    for item in cast(list[object], value):
        if isinstance(item, dict):
            entry = cast(dict[str, object], item)
            names.append(str(entry.get("label") or entry.get("name") or "recipient"))
        else:
            names.append(str(item))
    return ", ".join(_one_line(name) for name in names)


_WRITE_VERBS: Final[dict[str | None, str]] = {
    "upload": "Upload or replace a file",
    "workbook": "Change an Excel workbook",
    "onenote_create": "Add a OneNote page",
    "onenote_update": "Change a OneNote page",
    None: "Change something",
}


def _microsoft_write_action(method: str, path: str) -> str:
    """What a `microsoft_write` does, in words, as the tool itself classifies the call."""
    return f"{_WRITE_VERBS[write_kind(method.upper(), path)]} in your Microsoft 365"


def _write_body_text(body: object) -> str:
    """A `microsoft_write` body as text: a text body as is, JSON in its compact form."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, default=str)


def write_body_size(body: object) -> tuple[int, int]:
    """How many characters, and UTF-8 bytes, of `microsoft_write` body the card has to show."""
    text = _write_body_text(body)
    return len(text), len(text.encode())


def _shown_write_body(text: str) -> str:
    """The part of a `microsoft_write` body the card shows: all of it, up to the tool's limits."""
    shown = text[:WRITE_BODY_LIMIT].encode()[:WRITE_BODY_BYTES]
    return shown.decode(errors="ignore")


def _microsoft_write_card(data: dict[str, object]) -> tuple[str, str, tuple[str, ...]]:
    """The action, always-visible text and Details of one `microsoft_write` approval."""
    method = _one_line(data.get("method")).upper()
    decoded = unquote(str(data.get("path") or ""))
    decoded = decoded.removeprefix("/v1.0") if decoded.lower().startswith("/v1.0/") else decoded
    path = _one_line(decoded)
    handle = _one_line(data.get("file_handle")) if data.get("file_handle") else None
    text = _write_body_text(data.get("body"))
    shown_text = _shown_write_body(text)
    lines = [line for line in map(_plain, shown_text.splitlines()) if line.strip()]
    facts = (
        f"Method: {method}",
        # The end of a path says what is changed, so a long one loses its middle.
        f"Path: {_clip_middle(path, _WRITE_PATH_PREVIEW)}",
        *((f"File handle: {_short(handle)}",) if handle else ()),
    )
    if lines:
        shown = (f"Content: {_preview(_one_line(' '.join(lines)))}",)
    else:
        # An upload's bytes are staged, not in the call; its file handle stands for them.
        shown = () if handle else ("Content: (none)",)
    cut = len(text) - len(shown_text)
    details = (
        f"Method: {method}",
        f"Path: {_clip_middle(path, WRITE_PATH_LIMIT)}",
        *((f"File handle: {_short(handle, limit=WRITE_PATH_LIMIT)}",) if handle else ()),
        *(("Content:", *lines) if lines else ()),
        *((f"… (+{cut} more characters not shown)",) if cut > 0 else ()),
    )
    consequence = "\n".join(("It acts as you in Microsoft 365.", *facts, *shown))
    return _microsoft_write_action(method, decoded), consequence, details


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
    if (
        call.tool_name in {"request_fleet_config", "request_provider_allowlist"}
        and server == DAIMON_SERVER_NAME
    ):
        if call.tool_name == "request_fleet_config":
            reference = ConfigurationReference.model_validate(
                {
                    "operation": "fleet_revoke" if data.get("revoke") else "fleet_set",
                    "agent_name": data.get("agent_name"),
                    "config": data.get("config"),
                    "expected_revision": data.get("expected_revision"),
                    "preview_digest": data.get("preview_digest"),
                }
            )
        else:
            reference = ConfigurationReference.model_validate(
                {
                    "operation": "provider_allowlist",
                    "providers": data.get("providers"),
                    "expected_revision": data.get("expected_revision"),
                    "preview_digest": data.get("preview_digest"),
                }
            )
        return ConfirmationPrompt(
            title="Change fleet settings?"
            if call.tool_name == "request_fleet_config"
            else "Change allowed providers?",
            requester_platform_user_id=requester_platform_user_id,
            expires_at=now + timeout,
            admin=True,
            configuration=reference,
        )
    if call.tool_name == "request_channel_pin_clear" and server == DAIMON_SERVER_NAME:
        reference = ConfigurationReference.model_validate(
            {
                "operation": "channel_pin_clear",
                "expected_revision": data.get("expected_revision"),
                "preview_digest": data.get("preview_digest"),
            }
        )
        return ConfirmationPrompt(
            title="Clear this channel's provider pin?",
            requester_platform_user_id=requester_platform_user_id,
            expires_at=now + timeout,
            admin=True,
            configuration=reference,
        )
    if call.tool_name == "request_agent_backend_switch" and server == DAIMON_SERVER_NAME:
        reference = BackendSwitchReference.model_validate(
            {
                "agent": data.get("agent_name"),
                "target_backend": data.get("target_backend"),
                # As the tool resolved it ("argon" -> its id), or the card's
                # preview names a model no agent can select.
                "model": _switch_model(data.get("target_backend"), data.get("model")),
                "expected_revision": data.get("expected_revision"),
                "preview_digest": data.get("preview_digest"),
                "accepted_drops": data.get("accepted_drops"),
                "thread_mode": data.get("thread_mode"),
                "required_capabilities": data.get("required_capabilities"),
            }
        )
        # No model-supplied compatibility text is accepted. The host hook must
        # reload and verify the preview before displaying this opt-in prompt.
        return ConfirmationPrompt(
            title="Change the model provider?",
            requester_platform_user_id=requester_platform_user_id,
            expires_at=now + timeout,
            admin=True,
            backend_switch=reference,
        )
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
            f"Link expires: {'Never' if data.get('permanent') else _in_days(days)}",
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
    elif call.tool_name == "microsoft_send" and server == DAIMON_SERVER_NAME:
        invite = data.get("action") == "create_event"
        # Each field is one line: a newline in one must not fake a line of the card.
        subject = _one_line(data.get("subject") or "")
        noun = "invite" if invite else "email"
        action = f'Send {noun} "{_short(subject, limit=100)}"'
        title = f"{action}?"
        denied_action = f"{noun.capitalize()} not sent"
        # Who and what is shown outside Details: the person approves exactly
        # that. The body is previewed there and given in full behind Details.
        body = str(data.get("body") or "")
        preview = _preview(_one_line(body))
        location = _one_line(data.get("location") or "")
        zone = _one_line(data.get("time_zone") or "UTC")
        heading = (
            f"To: {_recipients(data.get('to'))}",
            *(
                (f"{'Optional' if invite else 'Cc'}: {_recipients(data.get('cc'))}",)
                if data.get("cc")
                else ()
            ),
            f"Subject: {subject}",
            *(
                (f"When: {_one_line(data.get('start'))} to {_one_line(data.get('end'))} ({zone})",)
                if invite
                else ()
            ),
            *((f"Where: {location}",) if invite and location else ()),
        )
        lines = [line for line in map(_plain, body[:SEND_BODY_LIMIT].splitlines()) if line.strip()]
        detail_lines = (*heading, "Message:" if lines else "Message: (empty)", *lines)
        consequence = "\n".join(
            (
                "It goes out from your Microsoft account.",
                *heading,
                f"Message: {preview or '(empty)'}",
            )
        )
    elif call.tool_name == "microsoft_write" and server == DAIMON_SERVER_NAME:
        action, consequence, detail_lines = _microsoft_write_card(data)
        title = f"{action}?"
        denied_action = "Microsoft 365 not changed"
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
        # Every publish: a slug-only notebook re-upload can land on an existing
        # permanent or editable notebook, which the call's own flags don't show.
        lasting_public_link=publishing,
    )


def confirmed_tools_for_turn(deps: TurnDeps) -> frozenset[str]:
    """Daimon's tools this turn's session asks before, from already-injected selectors."""
    return backend_switch_tools(enabled=deps.backend_selection_enabled) | microsoft_send_tools(
        enabled=deps.microsoft_send, tool_safety_on=deps.tool_safety.enabled
    )


def stated_settings(
    thread_mode: ThreadMode | None, required_capabilities: tuple[Capability, ...] | None
) -> AgentSettings | None:
    """The settings a request states, or None to keep the agent's own."""
    from daimon.core.channel_backend import AgentSettings
    from mux.contracts.config import CapabilityRequirement

    if thread_mode is None and required_capabilities is None:
        return None
    required = CapabilityRequirement(level="required")
    return AgentSettings(
        requires={cap: required for cap in required_capabilities or ()} or None,
        thread_mode=thread_mode,
    )


def _switch_model(backend: object, model: object) -> object:
    """The tool call's model resolved as the tool resolves it; as sent when it can't be."""
    from daimon.core.agent_model_names import resolve_switch_model
    from mux.contracts.ids import PROVIDERS

    if backend not in PROVIDERS or not (model is None or isinstance(model, str)):
        return model
    try:
        return resolve_switch_model(backend, model)
    except ValueError:
        return model


def reference_settings(reference: BackendSwitchReference) -> AgentSettings | None:
    return stated_settings(reference.thread_mode, reference.required_capabilities)


#: Plain words for a declared loss on a provider-switch card.
_DROP_WORDS: Mapping[str, str] = {
    "credentials": "its saved keys",
    "memory": "its memory",
    "sandbox": "its sandbox",
    "skills": "its skills",
    "mcp": "its connected tools",
    "tools": "its tools",
    "files": "its files",
    "repository": "its repository",
    "model_controls": "its model settings",
    "builtin_tool:web_fetch": "web fetch",
}


def drop_words(drops: Sequence[str]) -> str:
    """'its saved keys, its memory and its sandbox': saved keys first, as the costliest."""
    words: list[str] = []
    for drop in sorted(drops, key=lambda item: (item != "credentials", item)):
        if drop.startswith("mcp_tool:"):
            _, server, tool = drop.split(":", 2)
            words.append(f"the {tool} tool of {server}")
        else:
            words.append(_DROP_WORDS.get(drop, drop.replace("_", " ")))
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def settings_line(settings: Mapping[str, object]) -> str:
    """Plain words for the agent's stated settings on a card."""
    parts: list[str] = []
    if settings.get("thread_mode") == "shared":
        parts.append("shares threads where the channel allows it")
    elif settings.get("thread_mode") == "per_caller":
        parts.append("one thread per person")
    requires = settings.get("requires")
    if isinstance(requires, Mapping) and requires:
        names = cast(Mapping[str, object], requires)
        parts.append("requires " + ", ".join(sorted(names)))
    if settings.get("profile"):
        parts.append(f"profile {settings['profile']}")
    return "; ".join(parts) or "the model provider's defaults"


class _SwitchedMeanwhile(ValueError):
    """Another switch of the agent landed after the card was posted; says where it is now."""


def backend_switch_callbacks(
    service: AgentBackendService,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    current_admin: Callable[[str], Awaitable[bool]],
    resource_host: NeutralResourceHost | None = None,
    default_agent_name: str | None = None,
    place_word: str = "server",
    pin_channel: ChannelRef | None = None,
    answering_agent_name: str | None = None,
    handoff_thread: tuple[str, str, str] | None = None,
) -> tuple[
    Callable[[ConfirmationPrompt], Awaitable[ConfirmationPrompt]],
    Callable[[ConfirmationPrompt, str], Awaitable[str | None]],
]:
    """Bind the existing card hook to trusted current identity and preview facts.

    Only the loader reads agent configuration. The UI never treats a model's
    compatibility claims or digest as authority; application remains the
    shared service's compare-and-set after the approved tool call.
    """
    from daimon.core.authz import build_subject
    from daimon.core.channel_backend import BackendActor, load_backend_agent
    from daimon.core.stores.accounts import get_account_with_tenant

    async def verified(
        prompt: ConfirmationPrompt, clicker: str
    ) -> tuple[BackendAgent, CompatibilityPreview]:
        reference = prompt.backend_switch
        if not prompt.admin or reference is None or clicker != prompt.requester_platform_user_id:
            raise ValueError("This confirmation is unavailable. Preview again.")
        async with sessionmaker() as session:
            identity = await get_account_with_tenant(session, account_id=account_id)
        if (
            identity is None
            or identity.tenant_id != tenant_id
            or identity.platform_user_id != clicker
        ):
            raise ValueError("This confirmation is unavailable. Preview again.")
        agent = await load_backend_agent(
            anthropic,
            sessionmaker,
            tenant_id=tenant_id,
            agent_name=reference.agent,
            resource_host=resource_host,
        )
        preview = await service.preview(
            agent, reference.target_backend, reference.model, reference_settings(reference)
        )
        if (
            preview.tenant_id == tenant_id
            and preview.agent_id == agent.agent_id
            and preview.setting_revision != reference.expected_revision
        ):
            # Another switch of this agent landed after this card was posted.
            from daimon.core.turn.notices import switched_meanwhile_text
            from mux.contracts.config import DEFAULT_BACKEND

            setting = await service.current(agent)
            raise _SwitchedMeanwhile(
                switched_meanwhile_text(
                    reference.agent,
                    (setting.backend if setting is not None else None) or DEFAULT_BACKEND,
                    reference.target_backend,
                )
            )
        if (
            preview.tenant_id != tenant_id
            or preview.agent_id != agent.agent_id
            or preview.target_backend != reference.target_backend
            or not preview.allowed
            or preview.digest != reference.preview_digest
            or (preview.declared_drops and reference.accepted_drops != preview.declared_drops)
            or (not preview.declared_drops and reference.accepted_drops not in (None, ()))
        ):
            raise ValueError("This preview changed. Preview and confirm again.")
        subject = build_subject(is_admin=await current_admin(clicker), platform_user_id=clicker)
        # The agent's owner or a current workspace admin, as the write re-checks.
        if not await service.may_edit(agent, BackendActor(tenant_id, account_id, subject)):
            raise ValueError("This change needs the agent's owner or a current workspace admin.")
        # Recheck the scoped identity after the platform lookup has yielded.
        async with sessionmaker() as session:
            current = await get_account_with_tenant(session, account_id=account_id)
        if current != identity:
            raise ValueError("This permission changed. Preview and confirm again.")
        return agent, preview

    async def verified_configuration(
        prompt: ConfirmationPrompt, clicker: str
    ) -> tuple[str, tuple[str, ...]]:
        from daimon.core.channel_backend import AgentBackendRefused, BackendActor

        reference = prompt.configuration
        if not prompt.admin or reference is None or clicker != prompt.requester_platform_user_id:
            raise ValueError("This confirmation is unavailable. Preview again.")
        async with sessionmaker() as session:
            identity = await get_account_with_tenant(session, account_id=account_id)
        if (
            identity is None
            or identity.tenant_id != tenant_id
            or identity.platform_user_id != clicker
        ):
            raise ValueError("This confirmation is unavailable. Preview again.")
        subject = build_subject(is_admin=await current_admin(clicker), platform_user_id=clicker)
        if (
            reference.operation in ("provider_allowlist", "channel_pin_clear")
            and not subject.is_admin
        ):
            raise ValueError("This change needs a current workspace admin.")
        actor = BackendActor(tenant_id=tenant_id, account_id=account_id, subject=subject)
        try:
            if reference.operation == "channel_pin_clear":
                # Only the chat this card is posted in: the reference names no channel.
                if pin_channel is None or pin_channel.tenant_id != str(tenant_id):
                    raise ValueError("This confirmation is unavailable. Preview again.")
                from daimon.core.channel_backend import current_backend
                from daimon.core.fleet_configuration import channel_pin_digest
                from daimon.core.stores import mux_state
                from daimon.core.turn.notices import provider_name

                async with sessionmaker() as session:
                    pin = await current_backend(session, pin_channel)
                    origin = (
                        await mux_state.config_origin(session, pin_channel, pin.local)
                        if pin is not None
                        else None
                    )
                if (
                    pin is None
                    or origin != "channel"
                    or pin.local != reference.expected_revision
                    or channel_pin_digest(pin) != reference.preview_digest
                ):
                    raise ValueError("This preview changed. Preview and confirm again.")
                model = f" ({pin.model})" if pin.model else ""
                action = "Clear this channel's provider pin"
                details = (
                    f"This channel pins {provider_name(pin.backend)}{model}.",
                    "After clearing, each agent answers here on its own provider. "
                    "Existing threads keep their model.",
                )
            elif reference.operation == "provider_allowlist":
                assert reference.providers is not None
                policy = await service.tenant_policy(tenant_id)
                if (
                    policy.revision != reference.expected_revision
                    or provider_preview_digest(policy, reference.providers)
                    != reference.preview_digest
                ):
                    raise ValueError("This preview changed. Preview and confirm again.")
                await service.set_tenant_allowlist(
                    tenant_id, reference.providers, policy.revision, actor, dry_run=True
                )
                action = "Change allowed providers"
                details = (
                    "Allowed providers: " + (", ".join(reference.providers) or "none"),
                    "Applies to this server. Saving makes this list the server's own choice: "
                    "it stops following the providers this Daimon install has keys for.",
                )
            else:
                assert reference.agent_name is not None
                agent = await load_backend_agent(
                    anthropic,
                    sessionmaker,
                    tenant_id=tenant_id,
                    agent_name=reference.agent_name,
                    resource_host=resource_host,
                )
                if not await service.may_edit(agent, actor):
                    raise ValueError("This change needs the agent's owner or a current admin.")
                setting = await service.fleet_setting(
                    tenant_id, agent.agent_id, now=datetime.now(prompt.expires_at.tzinfo)
                )
                previous = setting.base
                complete = (
                    await complete_fleet_config(
                        reference.config,
                        previous,
                        anthropic,
                        tenant_id=tenant_id,
                        default=setting.kind != "custom",
                    )
                    if reference.config
                    else None
                )
                value = (
                    await configured_fleet_grant(
                        complete,
                        service,
                        anthropic,
                        sessionmaker,
                        agent,
                        now=datetime.now(prompt.expires_at.tzinfo),
                    )
                    if complete
                    else None
                )
                if value is not None:
                    # A non-admin owner may only tighten; the write re-checks at the click.
                    await service.check_owner_fleet_ceiling(
                        agent, value, actor, now=datetime.now(prompt.expires_at.tzinfo)
                    )
                preview = await service.preview_fleet_grant(
                    agent, value, now=datetime.now(prompt.expires_at.tzinfo)
                )
                if (
                    preview.setting_revision != reference.expected_revision
                    or preview.digest != reference.preview_digest
                ):
                    raise ValueError("This preview changed. Preview and confirm again.")
                action = (
                    "Turn off fleet for "
                    if reference.operation == "fleet_revoke"
                    else "Configure fleet for "
                ) + agent.name
                details = (
                    *fleet_default_line(setting.kind, revoke=reference.operation == "fleet_revoke"),
                    *fleet_detail_lines(
                        preview,
                        worker_names=complete.worker_agents if complete else None,
                        # The default's fixed expiry isn't a limit an owner set.
                        previous=previous if setting.kind == "custom" else None,
                    ),
                )
        except AgentBackendRefused as exc:
            if exc.reason == "fleet_raise_admin_required":
                raise ValueError(configuration_refusal(exc.reason)) from exc
            raise ValueError("This preview changed. Preview and confirm again.") from exc
        async with sessionmaker() as session:
            current = await get_account_with_tenant(session, account_id=account_id)
        if current != identity:
            raise ValueError("This permission changed. Preview and confirm again.")
        return action, details

    async def _answering_here(turn_agent: str | None) -> str | None:
        """Who answers this conversation next: a hand-off's agent, else this turn's.

        The approved switch moves the conversation of the agent answering here (the
        hand-off's, once one was made), so the card must say so for that agent.
        """
        if handoff_thread is None:
            return turn_agent
        from daimon.core.stores import thread_agent_bindings

        platform, parent_channel_id, thread_id = handoff_thread
        async with sessionmaker() as session:
            handed = await thread_agent_bindings.get_binding(
                session,
                tenant_id=tenant_id,
                platform=platform,
                parent_channel_id=parent_channel_id,
                thread_id=thread_id,
            )
        if handed is not None and handed.kind == "handoff":
            return handed.responder_name
        return turn_agent

    async def prepare(prompt: ConfirmationPrompt) -> ConfirmationPrompt:
        if prompt.configuration is not None:
            action, details = await verified_configuration(
                prompt, prompt.requester_platform_user_id
            )
            return prompt.model_copy(
                update={
                    "title": action + "?",
                    "action": action,
                    "denied_action": "Settings unchanged",
                    "detail_lines": details,
                    "consequence": "\n".join(details),
                }
            )
        agent, preview = await verified(prompt, prompt.requester_platform_user_id)
        details = (
            *(
                ("Agent settings: " + settings_line(preview.target_settings),)
                if preview.target_settings
                else ()
            ),
            "Unsupported skills: " + (", ".join(preview.unsupported_skills) or "None"),
            "Unsupported connections: " + (", ".join(preview.unsupported_mcps) or "None"),
            "Unsupported capabilities: " + (", ".join(preview.unsupported_capabilities) or "None"),
            "Shared-thread limits: " + ("; ".join(preview.shared_thread_limits) or "None"),
            preview.cost_line,
        )
        from daimon.core.turn.notices import provider_name

        provider = provider_name(preview.target_backend)
        target = provider + (f" ({preview.target_model})" if preview.target_model else "")
        action = f"Switch {agent.name} to {target}"
        unavailable = (
            f"On {provider} it won't have {drop_words(preview.declared_drops)}"
            if preview.declared_drops
            else ""
        )
        answering = await _answering_here(answering_agent_name)
        reach = (
            f"Affects new conversations in every channel on this {place_word}"
            if default_agent_name is not None and agent.name == default_agent_name
            else f"Affects new conversations wherever {agent.name} answers"
        )
        return prompt.model_copy(
            update={
                "title": action + "?",
                "action": action,
                "denied_action": "Model provider unchanged",
                "detail_lines": details,
                "consequence": (
                    reach
                    + "; other existing threads keep their model."
                    + (
                        f" This conversation moves to {provider} from your next message "
                        "(unless it's shared or this channel pins its provider); earlier "
                        "messages here aren't carried over."
                        if answering == agent.name
                        else ""
                    )
                    + (
                        f"\n{unavailable}. Nothing is deleted; switch back any time."
                        if unavailable
                        else ""
                    )
                ),
            }
        )

    async def check(prompt: ConfirmationPrompt, clicker: str) -> str | None:
        try:
            if prompt.configuration is not None:
                await verified_configuration(prompt, clicker)
            else:
                await verified(prompt, clicker)
        except _SwitchedMeanwhile as exc:
            return str(exc)
        except ValueError:
            return "This preview or your permission changed. Preview and confirm again."
        return None

    return prepare, check


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

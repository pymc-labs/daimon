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
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, cast

from daimon.core.tool_safety import DAIMON_SERVER_NAME, PUBLISH_TOOLS, ToolCall
from daimon.core.tool_safety import backend_switch_tools as backend_switch_tools
from mux.contracts.ids import Provider
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.neutral_resources import NeutralResourceHost
    from daimon.core.stores.domain import BackendAgent, CompatibilityPreview
    from daimon.core.turn.deps import TurnDeps
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "CONFIRMATION_TIMEOUT",
    "ConfirmationAnswer",
    "ApprovedConfirmation",
    "ConfirmationHook",
    "ConfirmationPrompt",
    "BackendSwitchReference",
    "PendingConfirmations",
    "no_confirmation_surface",
    "prompt_for_tool_call",
]

ConfirmationAnswer = Literal["approved", "denied", "expired"]

#: How long a card waits for its button before the action is refused. The
#: session sits idle meanwhile (no model spend), and the per-turn ceiling
#: still bounds the whole turn.
CONFIRMATION_TIMEOUT: Final[timedelta] = timedelta(minutes=10)


class BackendSwitchReference(BaseModel):
    """Untrusted tool reference; hosts reload the bound server-side preview."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str = Field(min_length=1)
    target_backend: Provider
    expected_revision: int = Field(ge=0, strict=True)
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


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
    if call.tool_name == "request_agent_backend_switch" and server == DAIMON_SERVER_NAME:
        reference = BackendSwitchReference.model_validate(
            {
                "agent": data.get("agent_name"),
                "target_backend": data.get("target_backend"),
                "expected_revision": data.get("expected_revision"),
                "preview_digest": data.get("preview_digest"),
            }
        )
        # No model-supplied compatibility text is accepted. The host hook must
        # reload and verify the preview before displaying this opt-in prompt.
        return ConfirmationPrompt(
            title="Confirm model backend change?",
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


def backend_switch_tools_for_turn(deps: TurnDeps) -> frozenset[str]:
    """Read the turn's already-injected selector without another feature flag."""
    return backend_switch_tools(enabled=deps.backend_selection_enabled)


def backend_switch_callbacks(
    service: AgentBackendService,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    current_admin: Callable[[str], Awaitable[bool]],
    resource_host: NeutralResourceHost | None = None,
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
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.channel_skills import may_set_channel_skills
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
            or identity.is_external
        ):
            raise ValueError("This confirmation is unavailable. Preview again.")
        agent = await load_backend_agent(
            anthropic,
            sessionmaker,
            tenant_id=tenant_id,
            agent_name=reference.agent,
            resource_host=resource_host,
        )
        preview = await service.preview(agent, reference.target_backend)
        if (
            preview.tenant_id != tenant_id
            or preview.agent_id != agent.agent_id
            or preview.target_backend != reference.target_backend
            or not preview.allowed
            or preview.setting_revision != reference.expected_revision
            or preview.digest != reference.preview_digest
        ):
            raise ValueError("This preview changed. Preview and confirm again.")
        subject = build_subject(is_admin=await current_admin(clicker), platform_user_id=clicker)
        if not may_set_channel_skills(subject, ""):
            raise ValueError("This change needs a current workspace admin.")
        # Recheck the scoped identity after the platform lookup has yielded.
        async with sessionmaker() as session:
            current = await get_account_with_tenant(session, account_id=account_id)
        if current != identity:
            raise ValueError("This permission changed. Preview and confirm again.")
        return agent, preview

    async def prepare(prompt: ConfirmationPrompt) -> ConfirmationPrompt:
        agent, preview = await verified(prompt, prompt.requester_platform_user_id)
        details = (
            "Unsupported skills: " + (", ".join(preview.unsupported_skills) or "None"),
            "Unsupported MCPs: " + (", ".join(preview.unsupported_mcps) or "None"),
            "Unsupported capabilities: " + (", ".join(preview.unsupported_capabilities) or "None"),
            "Shared-thread limits: " + ("; ".join(preview.shared_thread_limits) or "None"),
            preview.cost_line,
        )
        action = f"Switch {agent.name} to {preview.target_backend}"
        return prompt.model_copy(
            update={
                "title": action + "?",
                "action": action,
                "denied_action": "Model backend unchanged",
                "detail_lines": details,
                "consequence": (
                    "Existing threads keep their backend; "
                    "channel overrides still apply to new threads."
                ),
            }
        )

    async def check(prompt: ConfirmationPrompt, clicker: str) -> str | None:
        try:
            await verified(prompt, clicker)
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

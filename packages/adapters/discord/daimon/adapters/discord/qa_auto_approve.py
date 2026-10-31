"""Staging-only QA auto-approve: a QA bot's own confirmation cards answer themselves.

An automated evaluator can't press Discord buttons, so a journey that waits on
a confirmation card stalls. On staging, a card whose REQUESTER is an
allow-listed QA bot is answered "approved" as if that requester had clicked
it: the same checks a real click runs decide (`_ConfirmationView.authorize`:
the admin re-check with the card's own digest, then requester, expiry and
single use). A refusal leaves the normal card to wait, exactly as today. A
card anyone else asked for is never touched.

Gate, all three or a normal card (checked cheapest first, so production never
asks the metadata server):

1. the requester is one of `discord.qa_bot_user_ids`;
2. `qa-auto-approve.json`, beside the provider runtime file (the confirm-click
   control directory), says exactly ``{"enabled": true}``, is not a symlink
   and is not writable from inside this container;
3. the GCE metadata server names a project ending in `-staging`, or one ending
   in `-dev` whose deployment name is `lane-preview` (the QA dev lane).

Publish cards (`lasting_public_link`: a report, any notebook upload, a file in
a notebook) always stay manual, so a prompt-injected QA turn can't publish
lasting public data.

Every attempt that passes the gate is logged and written to the security audit
log, approved or refused. A gate that fails for any reason means a normal card.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
from daimon.adapters.discord.confirm_click import (
    control_file_path,
    gcp_project_id,
    qa_host,
)
from daimon.core.stores.security_audit import append_event

if TYPE_CHECKING:
    from daimon.core.config import Settings
    from daimon.core.confirmation import ConfirmationPrompt
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["AUDIT_TOOL", "CONTROL_FILE_NAME", "NOTE", "audit", "card_kind", "enabled_for"]

log = structlog.get_logger(__name__)

AUDIT_TOOL = "discord_qa_auto_approve"
CONTROL_FILE_NAME = "qa-auto-approve.json"
#: The line an auto-approved card carries, so nobody mistakes it for a person's click.
NOTE = "Auto-approved for QA (staging)"

# The project id never changes for a process; a failed lookup is retried.
_project_id: str | None = None


def control_path() -> Path | None:
    """`qa-auto-approve.json` beside `confirm-click.json`, or None when unset."""
    sibling = control_file_path()
    return sibling.with_name(CONTROL_FILE_NAME) if sibling is not None else None


def load_control(path: Path | None) -> bool:
    """True only for a read-only, non-symlink file that is exactly ``{"enabled": true}``."""
    if path is None:
        return False
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return False
        if os.access(path, os.W_OK) or os.access(path.parent, os.W_OK):
            return False
        data: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return data == {"enabled": True}


async def _qa_host(environment_name: str) -> bool:
    global _project_id
    if _project_id is None:
        _project_id = await gcp_project_id()
    return qa_host(_project_id, environment_name)


async def enabled_for(settings: Settings | None, requester: str) -> bool:
    """Whether `requester`'s cards auto-approve here (see the module docstring)."""
    discord_settings: object = getattr(settings, "discord", None)
    qa_ids: tuple[str, ...] = getattr(discord_settings, "qa_bot_user_ids", None) or ()
    if requester not in qa_ids:
        return False
    if not load_control(control_path()):
        return False
    environment: object = getattr(settings, "environment", None)
    return await _qa_host(getattr(environment, "name", "") or "")


def card_kind(prompt: ConfirmationPrompt) -> str:
    """A closed name for what the card confirms, for the log and audit."""
    if prompt.backend_switch is not None:
        return "backend_switch"
    if prompt.configuration is not None:
        return prompt.configuration.operation
    if prompt.tool_rule is not None:
        return "tool"
    return "admin" if prompt.admin else "confirmation"


def prompt_digest(prompt: ConfirmationPrompt) -> str:
    """The card's exact terms, hashed: what the QA requester approved."""
    return hashlib.sha256(prompt.model_dump_json().encode()).hexdigest()


async def audit(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    *,
    tenant_id: uuid.UUID | None,
    prompt: ConfirmationPrompt,
    outcome: Literal["allowed", "denied", "error"],
    reason: str,
    channel_id: int | None,
) -> None:
    """Log the attempt and, when the server is known, write its audit row."""
    kind, digest = card_kind(prompt), prompt_digest(prompt)
    log.warning(
        "discord.qa_auto_approve",
        kind=kind,
        requester=prompt.requester_platform_user_id,
        digest=digest,
        outcome=outcome,
        reason=reason,
        channel_id=channel_id,
    )
    if sessionmaker is None or tenant_id is None:
        return
    preview = prompt.configuration.preview_digest if prompt.configuration is not None else None
    try:
        async with sessionmaker.begin() as session:
            await append_event(
                session,
                tenant_id=tenant_id,
                account_id=None,
                agent_id=None,
                platform="discord",
                platform_user_id=prompt.requester_platform_user_id,
                tool_name=AUDIT_TOOL,
                operation=kind,
                outcome=outcome,
                reason=(
                    f"{reason}; digest={digest}"
                    + (f"; preview_digest={preview}" if preview is not None else "")
                ),
                token_kind="qa_requester",
                target_channel_id=str(channel_id) if channel_id is not None else None,
            )
    except Exception:
        log.exception("security_audit.write_failed", tool=AUDIT_TOOL)

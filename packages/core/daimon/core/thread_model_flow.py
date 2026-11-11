"""One conversation's model pick as a chat card shows it: the choices, the preview, one Confirm.

Slack and Teams share these; Discord `/model` runs the same core calls
(`daimon.core.thread_model_switch`). A pick that changes provider returns what changes and
the digest it was shown at, which the platform keeps in its Confirm button; the confirm
reads the preview again and keeps the pick only if it still matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from daimon.core import backend_capture, channel_backend
from daimon.core.authz import build_subject
from daimon.core.config import load_turn_settings
from daimon.core.errors import DaimonError
from daimon.core.model_switch import SwitchRefusal, switch_fact_lines
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.thread_model_switch import (
    APPLIES_TO,
    ThreadPick,
    confirm_thread_or_refusal,
    pick_value,
    preview_thread_or_refusal,
    thread_model_picker,
    thread_pick_for,
)
from daimon.core.thread_models import choose_thread_model
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    import uuid

    from anthropic import AsyncAnthropic
    from daimon.core.config import Settings
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.domain import Platform
    from daimon.core.turn.deps import TurnDeps
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NO_AGENT: Final = "No agent answers in this conversation yet."
NOTHING_CHANGED: Final = "Nothing changed."


@dataclass(frozen=True)
class ThreadOption:
    """One entry of the model select."""

    label: str
    value: str


LEGACY_OPTIONS: Final = (
    ThreadOption("Haiku", "haiku"),
    ThreadOption("Sonnet", "sonnet"),
    ThreadOption("Opus", "opus"),
    ThreadOption("Agent default", "default"),
)


@dataclass(frozen=True)
class ThreadConfirm:
    """A pick waiting for its one Confirm: the value and what it was previewed at."""

    value: str
    digest: str
    revision: int


@dataclass(frozen=True)
class ThreadPickResult:
    """What a pick came to: the words to show, and the Confirm when one is needed.

    `lines` are the preview's facts when `confirm` is set; `text` is the one-line answer
    (kept, or refused) otherwise.
    """

    text: str
    confirm: ThreadConfirm | None = None
    lines: tuple[str, ...] = ()


async def thread_actor(
    *,
    turn_deps: TurnDeps | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    tenant_id: uuid.UUID,
    platform: Platform,
    user_id: str,
    admin: bool,
) -> tuple[channel_backend.BackendActor, channel_backend.AgentBackendService]:
    """Who picks, and the backend service that previews and keeps their pick."""
    async with sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=user_id
        )
        await session.commit()
    subject = build_subject(is_admin=admin, platform_user_id=user_id)
    actor = channel_backend.BackendActor(
        tenant_id=tenant_id, account_id=principal.account_id, subject=subject
    )
    service = channel_backend.make_backend_service(
        sessionmaker,
        settings.turn if settings is not None else load_turn_settings(),
        capture_source=backend_capture.for_agent(
            turn_deps, account_id=principal.account_id, subject=subject
        ),
    )
    return actor, service


async def build_thread_pick(
    *,
    turn_deps: TurnDeps | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    settings: Settings,
    default: DeploymentDefault,
    tenant_id: uuid.UUID,
    platform: Platform,
    user_id: str,
    admin: bool,
    channel_id: str,
    conversation_id: str,
) -> ThreadPick | None:
    """The pick for the agent answering in this conversation; None when none answers."""
    actor, service = await thread_actor(
        turn_deps=turn_deps,
        sessionmaker=sessionmaker,
        settings=settings,
        tenant_id=tenant_id,
        platform=platform,
        user_id=user_id,
        admin=admin,
    )
    return await thread_pick_for(
        service=service,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        settings=settings,
        default=default,
        actor=actor,
        platform=platform,
        channel_id=channel_id,
        thread_id=conversation_id,
        platform_user_id=user_id,
    )


async def thread_options(settings: Settings, pick: ThreadPick | None) -> tuple[ThreadOption, ...]:
    """The model select: the catalog with prices where provider choice is on, else the words."""
    if pick is None or not channel_backend.backend_selection_enabled(settings.turn):
        return LEGACY_OPTIONS
    try:
        picker = await thread_model_picker(pick)
    except (DaimonError, SQLAlchemyError):
        return LEGACY_OPTIONS
    offered = tuple(
        ThreadOption(f"{c.label} ({c.price})" if c.price else c.label, c.value)
        for c in picker.limited()
    )
    return (*offered, LEGACY_OPTIONS[-1])


async def pick_thread_model(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    pick: ThreadPick | None,
    tenant_id: uuid.UUID,
    platform: str,
    conversation_id: str,
    choice: str,
    admin: bool,
) -> ThreadPickResult:
    """Keep a pick that needs no consent; otherwise preview what changes for one Confirm."""
    word = choice.strip().lower()
    if word == "default" or not channel_backend.backend_selection_enabled(settings.turn):
        change = await choose_thread_model(
            sessionmaker,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=conversation_id,
            choice=choice,
        )
        return ThreadPickResult(change.note)
    if pick is None:
        return ThreadPickResult(NO_AGENT)
    value = pick_value(choice)
    shown = await preview_thread_or_refusal(pick, value, admin=admin)
    if isinstance(shown, SwitchRefusal):
        return ThreadPickResult(f"{shown.line} {NOTHING_CHANGED}")
    if shown.preview is None:
        done = await confirm_thread_or_refusal(
            pick,
            value,
            thread_id=conversation_id,
            digest=shown.digest,
            revision=shown.revision,
            admin=admin,
        )
        return ThreadPickResult(done.line if isinstance(done, SwitchRefusal) else done.note)
    lines = switch_fact_lines(shown, agent_name=None, applies_to=APPLIES_TO)
    return ThreadPickResult(
        "", ThreadConfirm(value, shown.digest, shown.revision) if shown.allowed else None, lines
    )


async def confirm_thread_pick(
    pick: ThreadPick, confirm: ThreadConfirm, *, conversation_id: str, admin: bool
) -> str:
    """Keep the confirmed pick; a refusal (or a preview that moved on) changes nothing."""
    done = await confirm_thread_or_refusal(
        pick,
        confirm.value,
        thread_id=conversation_id,
        digest=confirm.digest,
        revision=confirm.revision,
        admin=admin,
    )
    if isinstance(done, SwitchRefusal):
        return f"{done.line} {NOTHING_CHANGED}"
    return done.note

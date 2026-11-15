"""The one place a refused provider change gets its words, and the way out.

Every surface that shows an `AgentBackendRefused` (panels, cards, chat tools)
asks `refusal_card` for the line and the action to offer. Nobody formats the
exception itself: its text is a label, not copy.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final, Literal

from daimon.core.agent_record_ports import DAIMON_ID_PREFIXES
from daimon.core.anthropic_guard import anthropic_configured
from daimon.core.channel_backend import (
    REFUSAL_DETAILS,
    AgentBackendRefused,
    BackendActor,
    BackendUnsupported,
    channel_ref,
    clear_channel_backend,
    current_backend,
)
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.errors import UserFacingError
from daimon.core.stores import mux_state
from daimon.core.turn.notices import provider_name
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ModelRef

if TYPE_CHECKING:
    from daimon.core.agent_editing import AgentActor, AgentEditDeps
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.stores.domain import BackendAgent
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: What a refusal card can offer next. `None` means nothing the person can press.
RefusalAction = Literal[
    "allowlist",
    "switch_back_to_claude",
    "pick_another_model",
    "fresh_preview",
    "confirm_again",
]

CONFIRM_AGAIN_LINE: Final = "Confirm this switch again."

#: Lines for refusals that need no provider name, with their action.
_LINES: Final[dict[str, tuple[str, RefusalAction | None]]] = {
    "re_confirm": (CONFIRM_AGAIN_LINE, "confirm_again"),
    "drops_required": ("Review what changes first.", "fresh_preview"),
    "drops_mismatch": ("Review what changes first.", "fresh_preview"),
    "stale_preview": ("Review what changes first.", "fresh_preview"),
    "stale_revision": ("This changed since you looked. Review it again.", "fresh_preview"),
    "admin_required_for_shared_threads": ("A shared thread needs an admin to switch.", None),
    "admin_required": ("Only an admin can do that.", None),
    "owner_or_admin_required": ("Only the agent's owner or an admin can do that.", None),
    "model_not_selectable": ("That model isn't available. Pick another.", "pick_another_model"),
    "profile_not_selectable": ("That model isn't available. Pick another.", "pick_another_model"),
    "effort_not_offered": ("That model has no effort setting. Leave it on the default.", None),
    "selfhosted_requires_channel_configuration": (
        "This agent can't use that self-hosted model until an operator turns it on for it.",
        None,
    ),
    "channel_pin_lacks_capability": (
        "This channel's pinned provider can't run that here.",
        "switch_back_to_claude",
    ),
    "source_unsupported": ("That provider can't carry everything this agent uses.", None),
    "source_context_unavailable": (
        "A switch can't be confirmed from here. Ask in a server channel or thread.",
        None,
    ),
    "tenant_mismatch": ("That agent belongs to another organisation.", None),
    "agent_not_found": ("There's no such agent here.", None),
    "agent_identity_required": ("This needs an agent's own identity.", None),
    "actor_not_found": ("Your account wasn't found here. Send a message first, then retry.", None),
    "admin_attestation_unavailable": ("Your admin status couldn't be checked. Try again.", None),
    "no_previous_revision": ("There's no earlier setting to go back to.", None),
    "preview_unavailable": ("The preview couldn't be built. Try again.", "fresh_preview"),
    "revision_busy": ("Another change is in progress. Retry in a moment.", None),
}

#: Reasons that name the provider, or depend on who asks, or on a detail.
_SPECIAL: Final = frozenset({"backend_not_allowed", "feature_disabled", "source_unavailable"})

#: Every reason `refusal_card` words.
REFUSAL_REASONS: Final = frozenset(_LINES) | _SPECIAL

#: Why a setup couldn't be read or changed, by closed detail label.
_SOURCE_DETAILS: Final = {
    "identity": "This request doesn't come from the account that can change it here.",
    "source": "The agent's saved setup doesn't match the live agent.",
    "environment": "The agent's working environment is missing or archived.",
    "place": "This conversation's channel couldn't be verified.",
}

#: What each closed `detail` of a refusal says. Every one of `REFUSAL_DETAILS` has a line.
DETAIL_LINES: Final = {
    **_SOURCE_DETAILS,
    **dict.fromkeys(
        (
            "backend_mismatch",
            "losses_differ",
            "model_unpinned",
            "no_consent",
            "provisional_mismatch",
            "revert_differs",
            "source_drift",
            "source_identity",
            "source_pin",
        ),
        CONFIRM_AGAIN_LINE,
    ),
}

assert set(DETAIL_LINES) == REFUSAL_DETAILS, "every refusal detail needs a line"


def refusal_card(
    err: AgentBackendRefused | BackendUnsupported,
    *,
    provider: str | None = None,
    admin: bool = False,
) -> tuple[str, RefusalAction | None]:
    """The line to show for a refused provider change, and the action to offer.

    The line never ends in "Nothing changed."; the surface adds what it knows. An
    admin who can fix the cause gets an action; anyone else is told to ask one.
    """
    if isinstance(err, BackendUnsupported):
        said = str(err)
        if said.endswith(".") and " " in said and "_" not in said:
            # Copy written for people (a price window ended): only another model helps.
            return said, "pick_another_model"
        return f"{provider_name(provider)} can't run right now.", "switch_back_to_claude"
    named = provider_name(err.provider or provider)
    if err.reason == "backend_not_allowed":
        if admin:
            return f"{named} isn't turned on here.", "allowlist"
        return f"{named} isn't turned on here. Ask an admin.", None
    if err.reason == "feature_disabled":
        if (err.provider or provider) == "selfhosted":
            return "Self-hosted models aren't turned on here.", None
        return "Choosing a model provider is turned off here.", None
    if err.reason == "source_unavailable":
        if err.detail in _SOURCE_DETAILS:
            return _SOURCE_DETAILS[err.detail], None
        return "The agent's setup couldn't be read. Try again in a minute.", None
    return _LINES.get(err.reason, ("This change was refused.", None))


async def switch_back_to_claude(
    sessionmaker: async_sessionmaker[AsyncSession],
    agent: BackendAgent,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    actor: AgentActor,
    backend_actor: BackendActor,
    edit: AgentEditDeps,
    backends: AgentBackendService | None,
) -> str:
    """The way back: clear this channel's pin when it has one, else move the agent to Claude.

    A Daimon-owned record has no Claude twin to switch to, so on a keyed install an
    admin's press converts it back to a Managed Agents agent under the same name.
    """
    from daimon.core.agent_changes import change_model

    channel = channel_ref(tenant_id, platform, channel_id)
    async with sessionmaker.begin() as session:
        pin = await current_backend(session, channel)
        if pin is not None and await mux_state.config_origin(session, channel, pin.local) == (
            "channel"
        ):
            await clear_channel_backend(session, channel, actor=backend_actor, expected=pin)
            return "Cleared this channel's provider pin."
    if agent.source is not None and agent.source.ref.id.startswith(DAIMON_ID_PREFIXES):
        return await _convert_record_to_claude(sessionmaker, agent, actor=actor, edit=edit)
    await change_model(
        agent,
        ModelRef(provider=DEFAULT_BACKEND, id=DEFAULT_AGENT_MODEL),
        actor=actor,
        edit=edit,
        backends=backends,
    )
    return f"Switched back to {provider_name(DEFAULT_BACKEND)}."


async def _convert_record_to_claude(
    sessionmaker: async_sessionmaker[AsyncSession],
    agent: BackendAgent,
    *,
    actor: AgentActor,
    edit: AgentEditDeps,
) -> str:
    from daimon.core.agent_convert import convert_agent_to_anthropic, skill_receipt
    from daimon.core.agent_editing import AgentEditError

    if not anthropic_configured(edit.client):
        raise AgentEditError(
            "This install has no Claude key. Ask an admin to add one.", kind="unavailable"
        )
    if not actor.subject.is_admin:
        raise AgentEditError("Only an admin can move this agent to Claude.", kind="forbidden")
    try:
        converted = await convert_agent_to_anthropic(
            edit.client,
            sessionmaker,
            tenant_id=agent.tenant_id,
            name=agent.name,
            subject=actor.subject,
            public_url=edit.public_url,
            account_id=actor.account_id,
        )
    except UserFacingError as error:
        raise AgentEditError(str(error), kind="conflict") from error
    moved = f"Moved {agent.name} to {provider_name(DEFAULT_BACKEND)}."
    return " ".join((moved, *skill_receipt(converted)))

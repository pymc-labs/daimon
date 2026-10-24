"""The provider switch card Daimon posts itself, and what pressing it does.

A Claude turn shows the switch card through the provider's own tool
confirmation. An OpenAI or Gemini turn has none, so the MCP tool records the
previewed switch (`stores.backend_switch_cards`) and posts a card only its
requester can press, in the conversation the request came from. Pressing it
is a fresh request: the card is claimed once, then the same checks as the
Claude card's click run again (`confirmation.backend_switch_callbacks`) and
the switch goes through the same `set_backend` write.
"""

from __future__ import annotations

import datetime
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final, Literal

import structlog
from daimon.core.scope import DeploymentDefault
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from daimon.core.authz import Subject
    from daimon.core.config import TurnSettings
    from daimon.core.posted_controls import PostedCard
    from daimon.core.stores.backend_switch_cards import SwitchCardRow
    from daimon.core.turn.deps import TurnDeps

log = structlog.get_logger()

#: A switch card's buttons: ``bsw:<card id>:confirm`` or ``…:cancel``.
CUSTOM_ID_PATTERN: Final[str] = r"bsw:(?P<card>[0-9a-f-]{36}):(?P<action>confirm|cancel)"
_CUSTOM_ID = re.compile(CUSTOM_ID_PATTERN)

SwitchAction = Literal["confirm", "cancel"]


def card_custom_id(card_id: uuid.UUID, action: SwitchAction) -> str:
    return f"bsw:{card_id}:{action}"


def parse_custom_id(custom_id: str) -> tuple[uuid.UUID, SwitchAction] | None:
    match = _CUSTOM_ID.fullmatch(custom_id)
    if match is None:
        return None
    try:
        return uuid.UUID(match["card"]), match["action"]  # pyright: ignore[reportReturnType]
    except ValueError:
        return None


async def channel_pin_line(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
) -> str | None:
    """Say so when this channel pins a provider: the agent's switch won't apply there."""
    from daimon.core.channel_backend import channel_ref, current_backend
    from daimon.core.stores import mux_state
    from daimon.core.turn.notices import provider_name

    channel = channel_ref(tenant_id, platform, channel_id)
    async with sessionmaker() as session:
        pinned = await current_backend(session, channel)
        if (
            pinned is None
            or await mux_state.config_origin(session, channel, pinned.local) != "channel"
        ):
            return None
    model = f" ({pinned.model})" if pinned.model else ""
    return (
        f"This channel pins {provider_name(pinned.backend)}{model}, so this agent keeps "
        'answering on that here until an admin clears it: say "clear this channel\'s pin".'
    )


async def move_this_thread(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    responder_name: str | None,
    agent_name: str,
    target: Provider,
    pinned_here: bool,
) -> str:
    """After a confirmed switch, move the requester's own slot of this thread too.

    Only when the switched agent is the one answering here, and never a shared
    thread (it stays pinned for everyone in it). Says plainly what happens.
    """
    from daimon.core.stores import mux_state, thread_agent_bindings
    from daimon.core.thread_release import caller_slot, request_thread_release
    from daimon.core.turn.notices import provider_name

    provider = provider_name(target)
    if pinned_here:
        return "This channel pins its own provider, so this conversation stays on it."
    async with sessionmaker() as session:
        handed = await thread_agent_bindings.get_binding(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
        )
    # Who answers this conversation's next message: a hand-off's agent, else this
    # turn's responder. A setup conversation's configuration target never moves it.
    answering = (
        handed.responder_name
        if handed is not None and handed.kind in ("handoff", "owner")
        else responder_name
    )
    if (handed is not None and handed.kind == "setup") or agent_name != answering:
        return f"{agent_name} isn't the one answering here, so this conversation is unchanged."
    slot = caller_slot(
        tenant_id=tenant_id,
        platform=platform,
        channel_id=parent_channel_id,
        thread_id=thread_id,
        account_id=account_id,
    )
    async with sessionmaker.begin() as session:
        shared = slot.model_copy(update={"account_id": None})
        if await mux_state.get_binding(session, shared) is not None:
            return (
                "This conversation is shared, so it stays on its current model; "
                f"start a new thread to talk to {agent_name} on {provider}."
            )
        pinned = await mux_state.get_binding(session, slot, include_released=True)
        await request_thread_release(
            session,
            slot,
            generation=pinned.generation if pinned is not None else 0,
            now=datetime.datetime.now(datetime.UTC),
        )
    return (
        f"This conversation moves to {provider} from the next message; "
        "earlier messages here aren't carried over."
    )


#: A self-posted card lives outside any turn, so a turn's ceiling doesn't cap
#: it; it never waits past a day (#1414's longest card), and says when it ends.
SWITCH_CARD_MAX_S: Final[int] = 86400

#: The requested card's footer; each renderer fills the requester and the expiry.
SWITCH_FOOTER: Final[str] = "Only {requester} can confirm. Expires {expires}."


def requested_switch_card(
    card_id: uuid.UUID,
    *,
    title: str,
    facts: tuple[str, ...],
    requester_platform_user_id: str,
    expires_at: datetime.datetime,
) -> PostedCard:
    """The card as posted: what the switch does, and Confirm / Cancel for its requester."""
    from daimon.core.posted_controls import CardButton, PostedCard

    return PostedCard(
        kind="switch",
        state="requested",
        headline=f"🔁 {title}",
        facts=facts,
        buttons=(
            CardButton(
                label="Confirm", style="primary", custom_id=card_custom_id(card_id, "confirm")
            ),
            CardButton(
                label="Cancel", style="primary", custom_id=card_custom_id(card_id, "cancel")
            ),
        ),
        footer=SWITCH_FOOTER,
        requester_platform_user_id=requester_platform_user_id,
        expires_at_unix=int(expires_at.timestamp()),
    )


def answered_switch_card(press: SwitchCardPress) -> PostedCard:
    """The card after a press closed it: what happened, and no buttons."""
    from daimon.core.posted_controls import PostedCard

    if press.applied:
        return PostedCard(kind="switch", state="applied", headline=f"✅ {press.text}")
    if press.text == EXPIRED_TEXT:
        return PostedCard(kind="switch", state="expired", headline=f"⌛ {press.text}")
    return PostedCard(kind="switch", state="refused", headline=f"🛡️ {press.text}")


def _nothing_changed(text: str) -> str:
    return text if text.endswith("Nothing changed.") else f"{text} Nothing changed."


@dataclass(frozen=True)
class SwitchCardPress:
    """What a press did: the card's new words, and whether the card is now closed.

    An open (not closed) press leaves the card as it is and only answers the presser.
    """

    text: str
    closed: bool
    applied: bool = False


EXPIRED_TEXT: Final[str] = "This request ended before anyone clicked. Ask again to redo it."
ANSWERED_TEXT: Final[str] = "This card was already answered."
CANCELLED_TEXT: Final[str] = "Cancelled. Nothing changed."
CONFIRMING_TEXT: Final[str] = "This switch is already being confirmed."
FAILED_TEXT: Final[str] = (
    "Something went wrong confirming this switch. Ask which model it's on before trying again."
)


async def press_switch_card(
    *,
    deps: TurnDeps | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    turn_settings: TurnSettings,
    deployment_default: DeploymentDefault,
    tenant_id: uuid.UUID,
    card_id: uuid.UUID,
    action: SwitchAction,
    clicker: str,
    current_admin: Callable[[str], Awaitable[bool]],
    place_word: str,
    now: datetime.datetime,
    message_id: str,
) -> SwitchCardPress | None:
    """Answer one press. None: not the requester's (the card stays as it is).

    The requester is checked before the card is claimed, so nobody else's press
    can use it up. A claimed card is applied or refused exactly once. `deps`
    None reads no source (only for a service whose previews read none).
    """
    from daimon.core.stores import backend_switch_cards as cards

    async with sessionmaker() as session:
        card = await cards.get_card(session, tenant_id=tenant_id, card_id=card_id)
    if (
        card is None
        or card.requester_platform_user_id != clicker
        # Only the message the card was posted as can press it.
        or card.message_id != message_id
    ):
        return None
    if card.status == "claimed":
        # A Confirm is still applying: it edits the card when it is done.
        return SwitchCardPress(CONFIRMING_TEXT, closed=False)
    if card.status != "pending":
        return SwitchCardPress(card.outcome or ANSWERED_TEXT, closed=True)
    if action == "cancel":
        async with sessionmaker.begin() as session:
            cancelled = await cards.cancel(
                session, tenant_id=tenant_id, card_id=card_id, outcome=CANCELLED_TEXT
            )
        if cancelled:
            return SwitchCardPress(CANCELLED_TEXT, closed=True)
        # A Confirm claimed it first: never say "cancelled" over what that press does.
        async with sessionmaker() as session:
            now_card = await cards.get_card(session, tenant_id=tenant_id, card_id=card_id)
        if now_card is not None and now_card.status == "claimed":
            # Still applying: that press edits the card when it is done.
            return SwitchCardPress(CONFIRMING_TEXT, closed=False)
        return SwitchCardPress(
            (now_card.outcome if now_card is not None else None) or ANSWERED_TEXT, closed=True
        )
    async with sessionmaker.begin() as session:
        claimed = await cards.claim(session, tenant_id=tenant_id, card_id=card_id, now=now)
    if claimed is None:
        expired = card.expires_at <= now
        return SwitchCardPress(EXPIRED_TEXT if expired else ANSWERED_TEXT, closed=True)

    async def end(status: Literal["applied", "refused"], text: str) -> SwitchCardPress:
        async with sessionmaker.begin() as session:
            await cards.finish(session, card_id=card_id, status=status, outcome=text)
        return SwitchCardPress(text, closed=True, applied=status == "applied")

    try:
        return await _apply(
            claimed,
            end,
            deps=deps,
            sessionmaker=sessionmaker,
            anthropic=anthropic,
            turn_settings=turn_settings,
            deployment_default=deployment_default,
            tenant_id=tenant_id,
            clicker=clicker,
            current_admin=current_admin,
            place_word=place_word,
        )
    except Exception:
        # Claimed, so no other press will try: close it in plain words.
        log.exception("switch_card.apply_failed", card_id=str(card_id))
        return await end("refused", FAILED_TEXT)


async def _apply(
    claimed: SwitchCardRow,
    end: Callable[[Literal["applied", "refused"], str], Awaitable[SwitchCardPress]],
    *,
    deps: TurnDeps | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    turn_settings: TurnSettings,
    deployment_default: DeploymentDefault,
    tenant_id: uuid.UUID,
    clicker: str,
    current_admin: Callable[[str], Awaitable[bool]],
    place_word: str,
) -> SwitchCardPress:
    from daimon.core.authz import Place, build_subject
    from daimon.core.channel_backend import (
        AgentBackendRefused,
        BackendActor,
        BackendUnsupported,
        load_backend_agent,
        make_backend_service,
    )
    from daimon.core.confirmation import (
        BackendSwitchReference,
        ConfirmationPrompt,
        backend_switch_callbacks,
        reference_settings,
    )
    from daimon.core.scope import ScopeContext
    from daimon.core.turn.admission import capture_backend_source
    from daimon.core.turn.notices import provider_name, switched_meanwhile_text

    card_id = claimed.id

    platform_admin = current_admin

    async def admin_now(user: str) -> bool:
        # A requester from another organisation is never an admin here.
        return not claimed.client and await platform_admin(user)

    is_admin = await admin_now(clicker)
    subject = build_subject(is_admin=is_admin, platform_user_id=clicker, client=claimed.client)
    if "operation" in claimed.reference:
        return await _apply_configuration(
            claimed,
            end,
            deps=deps,
            sessionmaker=sessionmaker,
            anthropic=anthropic,
            turn_settings=turn_settings,
            deployment_default=deployment_default,
            tenant_id=tenant_id,
            clicker=clicker,
            admin_now=admin_now,
            subject=subject,
            place_word=place_word,
        )
    reference = BackendSwitchReference.model_validate(claimed.reference)
    # The source is read in the card's own conversation, as the MCP tool read it
    # for the preview, so the same agent setup yields the same preview digest.
    capture = (
        None
        if deps is None
        else partial(
            capture_backend_source,
            deps,
            context=ScopeContext(
                tenant_id=tenant_id,
                account_id=claimed.account_id,
                platform=claimed.platform,
                channel_id=claimed.channel_id,
                thread_id=claimed.thread_id,
            ),
            subject=subject,
            place=Place.from_origin(
                parent_channel_id=claimed.channel_id,
                thread_id=claimed.thread_id,
                setup_thread=claimed.setup_thread,
            ),
            is_dm=False,
            slack_turn_context_id=None,
            private_dm_id=None,
        )
    )
    service = make_backend_service(
        sessionmaker,
        turn_settings,
        capture_source=capture,
    )
    _, check = backend_switch_callbacks(
        service,
        sessionmaker=sessionmaker,
        anthropic=anthropic,
        tenant_id=tenant_id,
        account_id=claimed.account_id,
        current_admin=admin_now,
        default_agent_name=deployment_default.agent_name,
        place_word=place_word,
        answering_agent_name=claimed.answering_agent_name,
    )
    prompt = ConfirmationPrompt(
        title="Change the model provider?",
        requester_platform_user_id=claimed.requester_platform_user_id,
        expires_at=claimed.expires_at,
        admin=True,
        backend_switch=reference,
    )
    refusal = await check(prompt, clicker)
    if refusal is not None:
        return await end("refused", _nothing_changed(refusal))
    actor = BackendActor(tenant_id=tenant_id, account_id=claimed.account_id, subject=subject)
    try:
        agent = await load_backend_agent(
            anthropic, sessionmaker, tenant_id=tenant_id, agent_name=reference.agent
        )
    except AgentBackendRefused:
        return await end("refused", f"{reference.agent} is no longer available. Nothing changed.")
    try:
        await service.set_backend(
            agent,
            reference.target_backend,
            reference.expected_revision,
            actor,
            expected_preview_digest=reference.preview_digest,
            model=reference.model,
            settings=reference_settings(reference),
            **(
                {"accepted_drops": reference.accepted_drops}
                if reference.accepted_drops is not None
                else {}
            ),
        )
    except AgentBackendRefused as exc:
        log.warning("switch_card.refused", card_id=str(card_id), reason=exc.reason)
        if exc.reason == "stale_revision":
            current = await service.current(agent)
            return await end(
                "refused",
                switched_meanwhile_text(
                    reference.agent,
                    current.backend if current is not None else None,
                    reference.target_backend,
                )
                + " Nothing changed.",
            )
        return await end(
            "refused", "This switch was refused when it was confirmed. Nothing changed."
        )
    except BackendUnsupported:
        log.warning("switch_card.unsupported", card_id=str(card_id))
        return await end(
            "refused",
            f"{provider_name(reference.target_backend)} can't run here right now. Nothing changed.",
        )
    pin = await channel_pin_line(
        sessionmaker, tenant_id=tenant_id, platform=claimed.platform, channel_id=claimed.channel_id
    )
    moved = await move_this_thread(
        sessionmaker,
        tenant_id=tenant_id,
        account_id=claimed.account_id,
        platform=claimed.platform,
        parent_channel_id=claimed.channel_id,
        thread_id=claimed.thread_id,
        responder_name=claimed.answering_agent_name,
        agent_name=reference.agent,
        target=reference.target_backend,
        pinned_here=pin is not None,
    )
    log.info("switch_card.applied", card_id=str(card_id), target=reference.target_backend)
    return await end(
        "applied",
        f"Switched {reference.agent} to {provider_name(reference.target_backend)}. {moved}",
    )


async def _apply_configuration(
    claimed: SwitchCardRow,
    end: Callable[[Literal["applied", "refused"], str], Awaitable[SwitchCardPress]],
    *,
    deps: TurnDeps | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    turn_settings: TurnSettings,
    deployment_default: DeploymentDefault,
    tenant_id: uuid.UUID,
    clicker: str,
    admin_now: Callable[[str], Awaitable[bool]],
    subject: Subject,
    place_word: str,
) -> SwitchCardPress:
    """A configuration card's press: the Claude card's own check, then the same write."""
    from daimon.core.channel_backend import (
        AgentBackendRefused,
        BackendActor,
        channel_ref,
        clear_channel_backend,
        current_backend,
        load_backend_agent,
        make_backend_service,
    )
    from daimon.core.confirmation import ConfirmationPrompt, backend_switch_callbacks
    from daimon.core.fleet_configuration import (
        ConfigurationReference,
        complete_fleet_config,
        configuration_refusal,
        configured_fleet_grant,
    )
    from daimon.core.turn.admission import capture_agent_source

    reference = ConfigurationReference.model_validate(claimed.reference)
    fleet = reference.operation in ("fleet_set", "fleet_revoke")
    # A fleet preview read the named agent's own source (the MCP tool's capture).
    capture = (
        partial(capture_agent_source, deps, account_id=claimed.account_id, subject=subject)
        if fleet and deps is not None
        else None
    )
    service = make_backend_service(sessionmaker, turn_settings, capture_source=capture)
    pin = channel_ref(tenant_id, claimed.platform, claimed.channel_id)
    _, check = backend_switch_callbacks(
        service,
        sessionmaker=sessionmaker,
        anthropic=anthropic,
        tenant_id=tenant_id,
        account_id=claimed.account_id,
        current_admin=admin_now,
        default_agent_name=deployment_default.agent_name,
        place_word=place_word,
        pin_channel=pin,
    )
    prompt = ConfirmationPrompt(
        title="Change this setting?",
        requester_platform_user_id=claimed.requester_platform_user_id,
        expires_at=claimed.expires_at,
        admin=True,
        configuration=reference,
    )
    refusal = await check(prompt, clicker)
    if refusal is not None:
        return await end("refused", _nothing_changed(refusal))
    actor = BackendActor(tenant_id=tenant_id, account_id=claimed.account_id, subject=subject)
    now = datetime.datetime.now(datetime.UTC)
    stale = "This changed since the card was posted. Ask again to redo it. Nothing changed."
    try:
        if fleet:
            assert reference.agent_name is not None
            assert isinstance(reference.expected_revision, int)
            agent = await load_backend_agent(
                anthropic, sessionmaker, tenant_id=tenant_id, agent_name=reference.agent_name
            )
            if reference.operation == "fleet_revoke" or reference.config is None:
                await service.revoke_fleet_grant(
                    agent,
                    reference.expected_revision,
                    actor,
                    expected_preview_digest=reference.preview_digest,
                    now=now,
                )
                text = f"Fleet turned off for {agent.name}."
            else:
                setting = await service.fleet_setting(tenant_id, agent.agent_id, now=now)
                complete = await complete_fleet_config(
                    reference.config,
                    setting.base,
                    anthropic,
                    tenant_id=tenant_id,
                    default=setting.kind != "custom",
                )
                value = await configured_fleet_grant(
                    complete, service, anthropic, sessionmaker, agent, now=now
                )
                await service.set_fleet_grant(
                    agent,
                    value,
                    reference.expected_revision,
                    actor,
                    expected_preview_digest=reference.preview_digest,
                    now=now,
                )
                text = f"Fleet limits saved for {agent.name}."
        elif reference.operation == "provider_allowlist":
            providers = reference.providers or ()
            policy = await service.tenant_policy(tenant_id)
            if policy.revision != reference.expected_revision:
                return await end("refused", stale)
            await service.set_tenant_allowlist(tenant_id, providers, policy.revision, actor)
            text = "Allowed providers saved: " + (", ".join(providers) or "none") + "."
        else:
            async with sessionmaker.begin() as session:
                current = await current_backend(session, pin)
                if current is None or current.local != reference.expected_revision:
                    return await end("refused", stale)
                await clear_channel_backend(session, pin, actor=actor, expected=current)
            text = "This channel's provider pin is cleared: agents answer here on their own."
    except AgentBackendRefused as exc:
        log.warning("switch_card.configuration_refused", card_id=str(claimed.id), reason=exc.reason)
        return await end("refused", configuration_refusal(exc.reason) + " Nothing changed.")
    log.info("switch_card.configuration_applied", card_id=str(claimed.id), op=reference.operation)
    return await end("applied", text)

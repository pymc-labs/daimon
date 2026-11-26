"""Pick one conversation's model on any provider; it applies at once.

Every chat's thread pick and the MCP `set_thread_model` tool share `pick_thread`. Keeping
a pick stores this agent's run on that provider with the conversation, which
`thread_backend` reads on the next turn; what the provider can't carry is off there and
named in the note. A pick off the agent's provider also needs the agent's
`thread_providers` feature on, or its owner or an admin. A pick the model makes
(`set_thread_model`) that `moves_conversation` first previews (`preview_thread_pick`)
and applies on the person's card, naming the previewed `expected_revision`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from time import perf_counter
from typing import TYPE_CHECKING

import structlog
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_model_names import AmbiguousModel, model_name
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    BackendActor,
    BackendUnsupported,
    load_backend_agent,
)
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.model_switch import (
    ModelPicker,
    ModelSwitchPreview,
    SwitchRefusal,
    current_model,
    refusal_of,
)
from daimon.core.models_catalog import catalog_model, list_model_choices
from daimon.core.roster import load_roster
from daimon.core.stores import security_audit
from daimon.core.stores.thread_models import (
    get_thread_choice,
    put_thread_consent,
    set_thread_model,
)
from daimon.core.thread_models import THREAD_MODEL_CHOICES, ThreadModelChange
from daimon.core.turn.stage_timing import elapsed_ms, stage_timer
from mux.contracts.config import DEFAULT_BACKEND

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from daimon.core.config import Settings
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.domain import (
        AgentBackendRevision,
        BackendAgent,
        CompatibilityPreview,
        Platform,
    )
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ThreadPick:
    """Who picks, where: what every preview and confirm needs."""

    service: AgentBackendService
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings | None
    agent: BackendAgent
    actor: BackendActor
    #: The Managed Agents id the agent's features are kept under.
    ma_agent_id: str
    platform: str
    platform_user_id: str | None = None


async def thread_pick_for(
    *,
    service: AgentBackendService,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    default: DeploymentDefault,
    actor: BackendActor,
    platform: Platform,
    channel_id: str,
    thread_id: str,
    platform_user_id: str | None = None,
) -> ThreadPick | None:
    """The pick for the agent answering in this conversation; None when none answers.

    Chat platforms have no turn origin to read it from, so it comes from the same
    routing a reply uses.
    """
    async with sessionmaker() as session:
        roster = await load_roster(
            session,
            anthropic,
            tenant_id=actor.tenant_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=thread_id,
            default=default,
        )
    answering = roster.answering
    if answering is None:
        return None
    agent = await load_backend_agent(
        anthropic, sessionmaker, tenant_id=actor.tenant_id, agent_name=answering.name
    )
    if agent.agent_id != derive_agent_uuid(
        tenant_id=actor.tenant_id, ma_agent_id=answering.ma_agent_id
    ):
        raise AgentBackendRefused("agent_not_found")
    return ThreadPick(
        service=service,
        sessionmaker=sessionmaker,
        settings=settings,
        agent=agent,
        actor=actor,
        ma_agent_id=answering.ma_agent_id,
        platform=platform,
        platform_user_id=platform_user_id,
    )


def pick_value(choice: str) -> str:
    """The catalog value a typed word names (`sonnet`); a `provider:id` value stays as it is."""
    word = choice.strip().lower()
    model_id = THREAD_MODEL_CHOICES.get(word)
    return f"{DEFAULT_BACKEND}:{model_id}" if model_id is not None else choice.strip()


def _ambiguous(value: str) -> SwitchRefusal:
    return SwitchRefusal(
        f"{value} could mean more than one model. Pick one from the list.", None, "ambiguous"
    )


async def moves_conversation(pick: ThreadPick, provider: str, *, thread_id: str) -> bool:
    """Whether a pick sends the conversation to a provider neither it nor its agent runs on."""
    revision = await pick.service.current(pick.agent)
    agents = revision.backend if revision is not None and revision.backend else DEFAULT_BACKEND
    async with pick.sessionmaker() as session:
        choice = await get_thread_choice(
            session, tenant_id=pick.agent.tenant_id, platform=pick.platform, thread_id=thread_id
        )
    return provider not in {agents, choice.provider if choice is not None else agents}


async def preview_thread_pick(
    pick: ThreadPick, value: str, *, admin: bool
) -> ModelSwitchPreview | SwitchRefusal:
    """What a pick would turn off and the agent revision it was read at; keeps nothing."""
    try:
        shown, _runs = await _gated(pick, value)
        _run, preview = await _thread_run(pick, shown)
        return replace(shown, preview=preview)
    except (AgentBackendRefused, BackendUnsupported, AgentEditError) as error:
        return refusal_of(error, admin=admin)
    except AmbiguousModel:
        return _ambiguous(value)


async def _thread_run(
    pick: ThreadPick, shown: ModelSwitchPreview
) -> tuple[AgentBackendRevision, CompatibilityPreview]:
    claude = shown.model.provider == DEFAULT_BACKEND
    return await pick.service.thread_revision(
        pick.agent, None if claude else shown.model.provider, None if claude else shown.model.id
    )


async def pick_thread(
    pick: ThreadPick,
    value: str,
    *,
    thread_id: str,
    admin: bool,
    expected_revision: int | None = None,
) -> ThreadModelChange | SwitchRefusal:
    """A conversation's pick, kept at once like any model change, or refused with why.

    `expected_revision` refuses it as stale when the agent changed since its preview.
    Logs one `thread_model.pick_timing` line: its stages, outcome and providers.
    """
    started, stage_ms = perf_counter(), dict[str, float]()
    mark = stage_timer(stage_ms)
    done: ThreadModelChange | SwitchRefusal | None = None
    shown: ModelSwitchPreview | None = None
    try:
        try:
            shown, runs = await _gated(pick, value)
            mark("gates")
            run = None
            if runs:
                # One capture: the run's own preview is the one shown, not a second read.
                run, preview = await _thread_run(pick, shown)
                if expected_revision not in (None, preview.setting_revision):
                    raise AgentBackendRefused("stale_revision")
                shown = replace(shown, preview=preview)
                mark("capture_and_preview")
            done = await _keep(pick, shown, run, thread_id)
            mark("apply")
        except (AgentBackendRefused, BackendUnsupported, AgentEditError) as error:
            done = refusal_of(error, admin=admin)
        except AmbiguousModel:
            done = _ambiguous(value)
        return done
    finally:
        _log.info(
            "thread_model.pick_timing",
            tenant_id=str(pick.agent.tenant_id),
            agent_id=str(pick.agent.agent_id),
            platform=pick.platform,
            thread_id=thread_id,
            backend_from=shown.before if shown is not None else None,
            backend_to=shown.model.provider if shown is not None else None,
            model=shown.model.id if shown is not None else None,
            outcome="applied"
            if isinstance(done, ThreadModelChange)
            else done.reason
            if done is not None
            else "failed",
            total_ms=elapsed_ms(started),
            stage_ms=stage_ms,
        )


async def thread_model_picker(pick: ThreadPick) -> ModelPicker:
    """Every model this tenant can run, priced, with the agent's own as current."""
    revision = await pick.service.current(pick.agent)
    current = current_model(
        pick.agent,
        revision.backend if revision is not None else None,
        revision.model if revision is not None else None,
    )
    service = pick.service
    policy = await service.tenant_policy(pick.agent.tenant_id) if service.enabled else None
    choices = list_model_choices(tenant=policy, default=current, agent_id=pick.agent.agent_id)
    return ModelPicker(choices=choices, current=current)


async def may_pick_provider(pick: ThreadPick) -> bool:
    """Whether this person may move the conversation off the agent's provider."""
    async with pick.sessionmaker() as session:
        place = FeaturePlace(pick.agent.tenant_id, pick.platform, agent_id=pick.ma_agent_id)
        if (await feature_in(session, pick.settings, place, "thread_providers")).on:
            return True
    return await pick.service.may_edit(pick.agent, pick.actor)


async def _gated(pick: ThreadPick, value: str) -> tuple[ModelSwitchPreview, bool]:
    """The pick past its gates, not yet previewed, and whether it needs a run (a preview)."""
    service = pick.service
    policy = await service.tenant_policy(pick.agent.tenant_id) if service.enabled else None
    model = catalog_model(value, tenant=policy, agent_id=pick.agent.agent_id)
    revision = await service.current(pick.agent)
    before = revision.backend if revision is not None and revision.backend else DEFAULT_BACKEND
    claude = model.provider == DEFAULT_BACKEND
    label = model_name(model)
    shown = ModelSwitchPreview(model=model, label=label, before=before, preview=None)
    if claude and before == DEFAULT_BACKEND:
        return shown, False
    if not service.enabled:
        raise AgentBackendRefused("backend_not_allowed", provider=model.provider)
    if model.provider != before and not await may_pick_provider(pick):
        raise AgentBackendRefused("owner_or_admin_required", provider=model.provider)
    return shown, True


async def _keep(
    pick: ThreadPick, shown: ModelSwitchPreview, run: AgentBackendRevision | None, thread_id: str
) -> ThreadModelChange:
    """Store `shown` and its `run` for the conversation; what it can't carry is off there."""
    tenant_id = pick.agent.tenant_id
    async with pick.sessionmaker.begin() as session:
        await set_thread_model(
            session,
            tenant_id=tenant_id,
            platform=pick.platform,
            thread_id=thread_id,
            model=shown.model.id,
            provider=shown.model.provider,
        )
        if run is not None:
            await put_thread_consent(
                session, platform=pick.platform, thread_id=thread_id, revision=run
            )
        await security_audit.append_event(
            session,
            tenant_id=tenant_id,
            account_id=pick.actor.account_id,
            agent_id=pick.agent.agent_id,
            platform=pick.platform,
            platform_user_id=pick.platform_user_id,
            tool_name="thread_model",
            operation="set",
            outcome="allowed",
            reason=_reason(shown),
            scope=f"thread_model:{_scope_id(tenant_id, pick.platform, thread_id)}",
        )
    note = f"This conversation runs on {shown.label} from the next reply."
    if shown.model.provider != shown.before:
        note += " Earlier messages carry over."
    note = " ".join((note, *shown.lines()))
    return ThreadModelChange(model_id=shown.model.id, note=note)


def _reason(shown: ModelSwitchPreview) -> str:
    drops = shown.preview.declared_drops if shown.preview is not None else ()
    reason = f"completed; model={shown.model.provider}:{shown.model.id}"
    return reason + ("; declared_drops=" + ",".join(drops) if drops else "")


def _scope_id(tenant_id: uuid.UUID, platform: str, thread_id: str) -> str:
    return f"{tenant_id}:{platform}:{thread_id}"

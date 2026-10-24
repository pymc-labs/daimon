"""Stage one of the two-stage turn chokepoint: `admit()`.

A single core call performs identity resolution, the channel -> tenant ->
deployment config cascade, MA resolve + SDK retrieve, the per-tenant balance
gate, the per-user monthly cap gate and the channel budget gate -- returning a
frozen `Admission` or raising a typed error. No boolean gate result crosses this boundary.

Gate ORDER is load-bearing and must not be reordered: identity -> channel
protection -> invoker policy -> cascade -> missing-config -> resolve/retrieve ->
agent pin -> channel isolation -> balance -> cap -> channel budget. A tenant that
is both over-balance and mis-configured must see the config error (matches both
adapters' inline sequences today). The access policy gates run before the
cascade so a refused turn learns nothing about the tenant's configuration and
never reaches an MA call.

Ported verbatim from `bot.py`'s inline pre-turn sequence (the reference
implementation). Both the Discord and Slack adapters now call `admit()` as
their pre-turn gate.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Literal

import structlog
from anthropic import APIError, APIStatusError
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core import channel_backend
from daimon.core.access_policy import (
    TenantAccessPolicy,
)
from daimon.core.authz import (
    Action,
    AgentRef,
    Place,
    Subject,
    Surface,
    authorize,
    build_agent_ref,
    build_subject,
    build_turn_place,
)
from daimon.core.billing import is_over_cap
from daimon.core.channel_backend import (
    BackendUnsupported,
    backend_is_enabled,
    channel_ref,
    check_backend,
    current_backend_and_sharing,
)
from daimon.core.channel_backend import (
    log_backend_unsupported as _log_backend_unsupported,
)
from daimon.core.channel_budget import is_over_channel_budget
from daimon.core.channel_budget_notice import spawn_budget_notice
from daimon.core.channel_skills import turn_channel_skills, turn_platform_skills
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import (
    MAResolverMissError,
    resolve_agent,
    resolve_environment,
    retrieve_agent_cached,
    retrieve_environment_cached,
)
from daimon.core.permissions import (
    agent_permissions,
    at_home,
    channel_permissions,
    dm_source_limited,
    home_of,
    limiting_ids_at,
)
from daimon.core.scope import ResolvedConfig, ScopeContext
from daimon.core.session_fence_retry import retry_fences
from daimon.core.session_snapshot import MaSkill
from daimon.core.setup_conversations import get_setup_agent, get_setup_responder
from daimon.core.stores import agent_backends, mux_state
from daimon.core.stores.access_policy import PolicyBusyError, load_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.core.stores.domain import AgentBackendRevision, BackendAgent, Role
from daimon.core.stores.identity import (
    find_platform_principal,
    get_or_create_platform_principal,
    mark_principal_foreign,
)
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from daimon.core.stores.scoped_config_write import clear_agent_references
from daimon.core.stores.shared_channels import is_channel_shared, mark_channel_shared
from daimon.core.stores.thread_sessions import get_thread_session_at
from daimon.core.tenant_balance import is_over_balance
from daimon.core.thread_release import apply_thread_release, release_due
from daimon.core.turn.approvals import daimon_rule_set_for
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import (
    AdmissionDenialReason,
    AdmissionDenied,
    BackendRefusalCause,
    DmSourceSealedError,
    MissingTurnConfigError,
    SessionBusyError,
)
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn_hold import RequesterOrg, TurnHold
from mux.contracts.admission import Admission as BackendAdmission
from mux.contracts.config import BackendConfig, ConfigRevision, ResolvedBackend, resolve_default
from mux.contracts.ids import ChannelRef, Provider, Scope, ThreadRef
from mux.errors import ScopeViolation
from mux.profiles import MANAGED_AGENTS
from mux.profiles.selfhosted import LOOP
from mux.state.lease import Slot
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "Admission",
    "AdmissionDenied",
    "AdmissionDecision",
    "AdmissionGrant",
    "AdmissionObserver",
    "DmSource",
    "MissingTurnConfigError",
    "admit",
    "backend_platform_skills",
    "reauthorize",
]


if TYPE_CHECKING:
    from daimon.core.turn.prepare import ProviderPreparationRequest

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DmSource:
    """Where a DM conversation was started from, re-checked by `reauthorize`."""

    channel_id: str | None
    thread_id: str | None
    thread_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdmissionGrant:
    """The facts an admission was decided on, kept so it can be decided again.

    `admit()` decides at admission time; the session is built, reused,
    replaced or recovered later -- after a compatibility check, a workspace
    transfer, or a dead-session recovery minutes on. `reauthorize` re-reads
    the policy and asks `authorize` the same questions at that moment, so a
    pin, seal, protection or invoker change made in between applies to the
    turn that is about to run (the model's decide-then-do race).
    """

    tenant_id: uuid.UUID
    subject: Subject
    surface: Surface
    turn_place: Place
    agent: AgentRef
    run_place: Place
    channel_id: str
    thread_id: str | None
    is_dm: bool
    # Set by the DM path: the conversation's source, whose seal is re-checked.
    dm_source: DmSource | None = None
    # The channel the turn is held to; every question asked again carries it, so
    # a hold never relaxes between admission and the moment the session is built.
    hold: TurnHold | None = None
    # The platform admitted on; `reauthorize` rereads the requester's foreign
    # marker and the channel's shared record with it. None only in hand-built grants.
    platform: str | None = None


@dataclass(frozen=True)
class Admission:
    """Everything `run_prepared_turn` needs to start a turn, resolved once."""

    account_id: uuid.UUID
    agent: BetaManagedAgentsAgent
    environment: BetaEnvironment
    config: ResolvedConfig
    # Turn from a sealed channel (or a thread under one), or from a DM when the
    # tenant asks for it: memory mounts must be read-only.
    memory_read_only: bool = False
    # The turn's agent may not publish freely (`AgentPermissions.publishes`): its
    # session's publish tools wait for the requester's Approve on a card.
    asks_before_publishing: bool = False
    # Daimon tools this agent has an ask or deny rule on, read when the turn is
    # admitted (empty while rules are off): their session tools wait for the
    # driver's decision, so the set is part of the tools the session is bound to.
    daimon_rule_tools: frozenset[str] = frozenset()
    # Some of those rules only the turn driver applies (any `ask`, a channel
    # `deny`); an agent-wide `deny` is also enforced by the MCP server, which
    # backends without an approval pause rely on.
    daimon_rules_need_driver: bool = False
    # The channel or thread itself is sealed (DM memory policy aside). Callers
    # that copy content out of the channel, such as /dm-me, must refuse.
    source_sealed: bool = False
    # A DM admitted from a channel shared with another organisation
    # (`dm_source_channel_id`): its history must not be copied into the DM either.
    source_shared: bool = False
    # Only private Slack orchestration assigns this signed, execution-specific grant.
    slack_turn_context_id: uuid.UUID | None = None
    # The channel the turn is held to (`daimon.core.turn_hold`): the requester is
    # from another organisation, or the channel is shared with one. Its session
    # gets a credential bound to that channel. None for an ordinary turn.
    hold: TurnHold | None = None
    # The origin channel is shared with another organisation (stamped on the turn origin).
    shared_channel: bool = False
    private_dm_id: str | None = None
    # The channel and thread the turn runs in, and every sealed id that seals
    # them (the channel and a thread sealed on its own): stamped on the session
    # so the transcript tools can apply the seal to it
    # (`daimon.core.session_seal.origin_stamp`).
    origin_channel_id: str | None = None
    origin_thread_id: str | None = None
    origin_seal_ids: frozenset[str] = frozenset()
    # Parent channel the turn's spend is attributed to; in a DM, the channel it
    # was moved from, or None. Apart from `origin_channel_id`, which drives the
    # seal: a DM is budgeted to its source channel but never runs there.
    budget_channel_id: str | None = None
    # The skills the session adds to the agent's, decided once so creating the
    # session and checking it for drift add the same ones: the platform's
    # (`turn_platform_skills`, on the Anthropic backend only), then the
    # channel's (`daimon.core.channel_skills`).
    channel_skills: tuple[MaSkill, ...] = ()
    # Which of `channel_skills` are the platform's, so a create the provider
    # refuses for one of them can run without it (`create_ma_session`).
    platform_skill_ids: frozenset[str] = frozenset()
    # What admission was decided on; `reauthorize` decides it again at the
    # moment the session is built. None only for hand-built test admissions.
    grant: AdmissionGrant | None = field(default=None, compare=False, repr=False)
    observation: TurnObservation | None = field(default=None, compare=False, repr=False)
    # The channel's backend configuration and its admission, when the channel
    # has one and `DAIMON_TURN__CHANNEL_BACKENDS` is on; None otherwise.
    backend_revision: ConfigRevision | None = field(default=None, compare=False, repr=False)
    backend: BackendAdmission | None = field(default=None, compare=False, repr=False)
    # Set by `bind_session` for a shared thread (`daimon.core.shared_threads`):
    # the synthetic owner of its session row. The session is then created and
    # refreshed with no caller credentials. None for every per-caller turn.
    shared_owner: uuid.UUID | None = field(default=None, compare=False, repr=False)
    # Whether any revision of the channel ever shared threads, read with the
    # revision itself; a channel never shared reads no thread binding.
    backend_ever_shared: bool = field(default=False, compare=False, repr=False)
    # Required whenever a revision exists; unconfigured legacy turns have neither.
    backend_revision_origin: Literal["channel", "agent"] | None = field(
        default=None, compare=False, repr=False
    )
    backend_footer_profile: str | None = field(default=None, compare=False, repr=False)
    backend_counterpart: AgentRevision | None = field(default=None, compare=False, repr=False)
    backend_agent_revision: AgentBackendRevision | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.backend_revision is not None and self.backend_revision_origin not in (
            "channel",
            "agent",
        ):
            _log_backend_unsupported("admission_origin")
            raise AdmissionDenied(reason="backend_unsupported")


def _backend_refusal(
    revision: ConfigRevision | None,
    origin: Literal["channel", "agent", "default"] | None,
    enabled_backends: tuple[Provider, ...] | None,
    error: Exception | None = None,
) -> AdmissionDenied:
    """A `backend_unsupported` refusal naming what to fix, for the refusal copy."""
    if (
        isinstance(error, channel_backend.AgentBackendRefused)
        and error.reason == "backend_not_allowed"
    ):
        # The agent's own provider, refused by the allowlist before any revision exists.
        return AdmissionDenied(
            reason="backend_unsupported", cause="provider_not_allowed", provider=error.provider
        )
    provider = revision.backend if revision is not None else None
    cause: BackendRefusalCause | None = None
    if (
        provider is not None
        and enabled_backends is not None
        and not backend_is_enabled(provider, enabled_backends)
    ):
        cause = "provider_not_allowed"
    elif origin == "channel":
        cause = "channel_pin"
    elif origin == "agent":
        cause = "agent_backend"
    return AdmissionDenied(reason="backend_unsupported", cause=cause, provider=provider)


def _require_backend_origin(
    selection: ResolvedBackend,
    origin: Literal["channel", "agent"],
    revision: ConfigRevision | None = None,
    *,
    agent_backends: bool = False,
    agent_revision: AgentBackendRevision | None = None,
) -> None:
    from daimon.core.turn.runtime_manifest import fixed_plan_origin_allowed

    if selection.profile != MANAGED_AGENTS.profile_id and not (
        fixed_plan_origin_allowed(origin) or (origin == "agent" and agent_backends)
    ):
        _log_backend_unsupported("backend_origin")
        raise AdmissionDenied(reason="backend_unsupported")


@dataclass(frozen=True)
class AdmissionDecision:
    """Observed host gate result; never a provider session or completed turn.

    Notices and refusal logs remain adapter-owned. ``account_id`` is present
    only when the existing observation has resolved it before the decision.
    """

    tenant_id: uuid.UUID
    platform: str
    external_user_id: str
    channel_id: str
    thread_id: str | None
    account_id: uuid.UUID | None
    reason: AdmissionDenialReason | None

    @property
    def admitted(self) -> bool:
        return self.reason is None


type AdmissionObserver = Callable[[AdmissionDecision], None]


def _observe_admission(observer: AdmissionObserver, decision: AdmissionDecision) -> None:
    # An explicitly installed observer is passive: its failure must not turn a
    # refusal into a different exception or revoke a successfully resolved grant.
    try:
        observer(decision)
    except Exception:
        _log.warning("turn.admission_observer_failed")


async def admit(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    channel_id: str,
    now: datetime,
    thread_id: str | None = None,
    role: Role | None = None,
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
    observer: AdmissionObserver | None = None,
    requester_org: RequesterOrg,
    requester_home_id: str | None = None,
    channel_shared: bool | None = None,
) -> Admission:
    """Admit a turn, or raise the typed reason it is refused.

    `requester_org` is the platform's word on which organisation the requester
    belongs to; every caller states it. `requester_home_id` names the home
    organisation of an `OTHER` requester. `channel_shared` is the platform's live
    signal that the channel is shared with another organisation (None: no signal).
    A client (another organisation, unknown, or one already recorded as foreign) is
    held to the channel it asked in, as is anyone in a shared channel.
    """
    observation = current_outcome.get() or TurnObservation(
        deps.sessionmaker, tenant_id, platform, channel_id, thread_id
    )
    observation.channel_id = channel_id
    observation.thread_id = thread_id
    try:
        with observation.activate():
            result = await admit_impl(
                deps,
                tenant_id=tenant_id,
                platform=platform,
                external_user_id=external_user_id,
                channel_id=channel_id,
                now=now,
                thread_id=thread_id,
                role=role,
                is_dm=is_dm,
                dm_source_channel_id=dm_source_channel_id,
                category_id=category_id,
                category_unresolved=category_unresolved,
                requester_org=requester_org,
                requester_home_id=requester_home_id,
                channel_shared=channel_shared,
            )
    except BaseException as exc:
        observation.finish(error=exc)
        if observer is not None and isinstance(exc, AdmissionDenied):
            _observe_admission(
                observer,
                AdmissionDecision(
                    tenant_id,
                    platform,
                    external_user_id,
                    channel_id,
                    thread_id,
                    observation.account_id,
                    exc.reason,
                ),
            )
        raise
    observation.account_id = result.account_id
    observation.agent_id = result.agent.id
    if observer is not None:
        _observe_admission(
            observer,
            AdmissionDecision(
                tenant_id,
                platform,
                external_user_id,
                channel_id,
                thread_id,
                result.account_id,
                None,
            ),
        )
    return replace(result, observation=observation)


def backend_platform_skills(
    platform_skills: tuple[MaSkill, ...], profile: str | None
) -> tuple[MaSkill, ...]:
    """The platform's skills a session on backend `profile` (None: the default) runs. Pure.

    Another backend than Managed Agents has no Anthropic built-in skills and
    carries a channel's skills only as a declared loss, so it gets none.
    """
    return platform_skills if profile in (None, MANAGED_AGENTS.profile_id) else ()


async def admit_impl(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    channel_id: str,
    now: datetime,
    thread_id: str | None = None,
    role: Role | None = None,
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    category_id: str | None = None,
    category_unresolved: bool = False,
    requester_org: RequesterOrg,
    requester_home_id: str | None = None,
    channel_shared: bool | None = None,
) -> Admission:
    """Run the full pre-turn gate sequence; raise instead of returning bool."""
    started = last_stage = perf_counter()
    stage_ms: dict[str, float] = {}

    def mark(stage: str) -> None:
        nonlocal last_stage
        current = perf_counter()
        stage_ms[stage] = round((current - last_stage) * 1000, 1)
        last_stage = current

    # --- Identity resolution ---
    async with deps.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session,
            tenant_id=tenant_id,
            platform=platform,
            external_id=external_user_id,
        )
        client = requester_org is not RequesterOrg.HOST or principal.foreign_home_id is not None
        await _record_requester(
            session,
            principal_id=principal.id,
            account_id=principal.account_id,
            requester_org=requester_org,
            requester_home_id=requester_home_id,
            role=role,
            already_foreign=principal.foreign_home_id is not None,
        )
        shared = False
        source_shared = False
        # A DM has no channel of its own to share; /dm-me asks about the channel
        # it copies history from.
        shared_place = dm_source_channel_id if is_dm else channel_id
        if shared_place is not None:
            shared = await _record_shared_channel(
                session,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=shared_place,
                requester_org=requester_org,
                channel_shared=channel_shared,
            )
            source_shared = is_dm and shared
            shared = shared and not is_dm
        await session.commit()
        mark("identity")
        # A confirmed provider switch from this thread moves the caller's own slot:
        # select as if released now, apply once the turn is admitted (below).
        released = False
        state_store = deps.state_store or mux_state.PostgresStateStore(deps.sessionmaker)
        own_slot = Slot(
            thread=ThreadRef(
                channel=channel_ref(tenant_id, platform, channel_id), thread_id=thread_id or ""
            ),
            account_id=str(principal.account_id),
        )
        if deps.backend_selection_enabled and thread_id is not None and not is_dm:
            released = await release_due(deps.sessionmaker, state_store, own_slot)
        if is_dm and client:
            # A client has no private conversation with Daimon.
            raise AdmissionDenied(reason="client_requester")
        hold = TurnHold(platform, channel_id, client) if (client or shared) and not is_dm else None
        # Read after the role commit, so a refused turn still records the role.
        policy = await load_access_policy(session, tenant_id=tenant_id)
        mark("policy_and_admins")

        # Refusals must happen before config is read. Keeping the session here
        # reuses its connection for the config cascade after authorization.
        # A held turn has no admin exemption anywhere in admission.
        subject = build_subject(
            is_admin=role is Role.ADMIN and hold is None,
            platform_user_id=external_user_id,
            client=client,
        )
        turn_place = build_turn_place(
            channel_id=channel_id,
            thread_id=thread_id,
            category_id=category_id,
            category_unresolved=category_unresolved,
        )
        _require_turn_start(policy, subject, turn_place, hold)
        mark("start_policy")

        if (observation := current_outcome.get()) is not None:
            observation.account_id = principal.account_id

        scope = ScopeContext(
            account_id=principal.account_id,
            tenant_id=tenant_id,
            channel_id=channel_id,
            platform=platform,
            thread_id=thread_id,
        )
        config = await resolve_config(session, context=scope, default=deps.deployment_default)
        # Feature-disabled turns retain the existing channel read and skip all
        # agent preference, materialization and thread-pinning work.
        channel = channel_ref(tenant_id, platform, channel_id) if deps.channel_backends else None
        backend_revision, backend_ever_shared = (
            await current_backend_and_sharing(session, channel)
            if channel is not None
            else (None, False)
        )
        configured_channel = backend_revision is not None
        selection_enabled = deps.backend_selection_enabled
        if (
            selection_enabled
            and channel is not None
            and backend_revision is not None
            and await mux_state.config_origin(session, channel, backend_revision.local)
            == "channel_clear"
        ):
            backend_revision = None
            configured_channel = False
        backend_revision_origin: Literal["channel", "agent"] | None = (
            "channel" if configured_channel else None
        )
        backend_footer_profile: str | None = None
        retained_revision: int | None = None
        if (
            selection_enabled
            and channel is not None
            and thread_id is not None
            and not is_dm
            and config.thread_binding_kind != "setup"
        ):
            thread = ThreadRef(channel=channel, thread_id=thread_id)
            slots = [Slot(thread=thread, account_id=str(principal.account_id))]
            if backend_ever_shared:
                slots.insert(0, Slot(thread=thread))
            for slot in slots:
                if released and slot.account_id is not None:
                    continue  # A due release: this caller's slot selects afresh.
                binding = (
                    await deps.state_store.get_binding(slot)
                    if deps.state_store is not None
                    else await mux_state.get_binding(session, slot)
                )
                if binding is None:
                    continue
                if binding.thread != slot.thread or binding.legacy_account_id != slot.account_id:
                    raise ScopeViolation(
                        binding.id, "selected binding differs from authorized thread"
                    )
                retained_revision = binding.config_revision
                try:
                    retained, _, source = await channel_backend.resolve_turn_backend(
                        session, channel, None, enabled=True, retained_revision=retained_revision
                    )
                except (channel_backend.AgentBackendRefused, BackendUnsupported) as error:
                    _log_backend_unsupported("retained_revision_lookup", error)
                    raise AdmissionDenied(reason="backend_unsupported") from error
                if retained_revision == 0:
                    default = resolve_default(BackendConfig())
                    if (binding.provider, binding.profile) != (default.backend, default.profile):
                        _log_backend_unsupported("retained_default_mismatch")
                        raise AdmissionDenied(reason="backend_unsupported")
                    backend_revision = None
                    backend_revision_origin = None
                    break
                if retained is None or (
                    retained.channel != thread.channel
                    or retained.local != retained_revision
                    or retained.backend != binding.provider
                    or retained.profile != binding.profile
                ):
                    _log_backend_unsupported("retained_binding_mismatch")
                    raise AdmissionDenied(reason="backend_unsupported")
                backend_revision = retained
                backend_revision_origin = source if source != "default" else None
                backend_footer_profile = retained.profile
                break
            if retained_revision is None and not released:
                # A caller's older session remains on its original default
                # backend even if neutral thread bindings did not yet exist.
                previous = await get_thread_session_at(
                    session,
                    tenant_id=tenant_id,
                    platform=platform,
                    thread_id=thread_id,
                    account_id=principal.account_id,
                    at=now,
                )
                if previous is not None:
                    if previous.channel_id not in (None, channel_id):
                        raise ScopeViolation(
                            previous.ma_session_id,
                            "session history differs from authorized channel",
                        )
                    retained_revision = 0
                    backend_revision = None
                    backend_revision_origin = None
        if selection_enabled and backend_revision is not None:
            backend_footer_profile = backend_revision.profile
        if selection_enabled and channel is not None and retained_revision == 0:
            # Check the retained default profile too; its removal must refuse
            # the thread instead of silently selecting a new provider.
            default = resolve_default(BackendConfig())
            if configured_channel:
                backend_footer_profile = default.profile
            retained_default = ConfigRevision.create(channel, 0, default)
            try:
                check_backend(retained_default, enabled_backends=policy.enabled_backends)
            except BackendUnsupported as error:
                _log_backend_unsupported("retained_default_policy", error)
                raise _backend_refusal(retained_default, None, policy.enabled_backends) from error
    mark("config")

    # --- Missing config check (before any MA call) ---
    if config.agent_name is None or config.environment_name is None:
        missing: tuple[Literal["agent", "environment"], ...]
        if config.agent_name is None and config.environment_name is None:
            missing = ("agent", "environment")
        elif config.agent_name is None:
            missing = ("agent",)
        else:
            missing = ("environment",)
        raise MissingTurnConfigError(
            missing=missing,
            agent_name_tier=config.agent_name_tier,
            environment_name_tier=config.environment_name_tier,
        )

    # A legacy agent preference must be refused before resolving its source
    # through Anthropic. The name lookup can only deny; it never selects a
    # backend or grants authority to another resource with the same name.
    if (
        selection_enabled
        and channel is not None
        and not configured_channel
        and retained_revision is None
    ):
        async with deps.sessionmaker() as session:
            if config.responder_ma_agent_id is not None:
                preference = await agent_backends.current(
                    session,
                    tenant_id,
                    derive_agent_uuid(
                        tenant_id=tenant_id, ma_agent_id=config.responder_ma_agent_id
                    ),
                )
                refused = preference is not None and preference.backend == LOOP.provider
            else:
                refused = await agent_backends.has_backend_preference_by_name(
                    session,
                    tenant_id=tenant_id,
                    agent_name=config.agent_name,
                    backend=LOOP.provider,
                )
        if refused:
            raise AdmissionDenied(
                reason="backend_unsupported", cause="agent_backend", provider=LOOP.provider
            )

    # --- Channel backend: its profile must meet the channel's configuration
    # and be runnable here. Before any MA call; a channel never configured has
    # no revision and skips it. ---
    try:
        backend = (
            check_backend(
                backend_revision,
                enabled_backends=policy.enabled_backends if selection_enabled else None,
            )
            if backend_revision is not None
            else None
        )
    except BackendUnsupported as err:
        _log_backend_unsupported("channel_policy", err)
        raise _backend_refusal(
            backend_revision,
            backend_revision_origin,
            policy.enabled_backends if selection_enabled else None,
        ) from err

    selfhosted = backend_revision is not None and backend_revision.backend == LOOP.provider
    if selfhosted:
        from daimon.core.turn.selfhosted_records import resolve_records

        agent, environment = await resolve_records(
            deps.sessionmaker,
            Scope(
                tenant_id=str(tenant_id),
                account_id=str(principal.account_id),
                principal_id=external_user_id,
                authorization_id="turn-admission",
            ),
            agent_name=config.agent_name,
            environment_name=config.environment_name,
            responder_id=config.responder_ma_agent_id,
        )
        agent_id, env_id = agent.id, environment.id
        if (observation := current_outcome.get()) is not None:
            observation.agent_id = agent.id
    else:

        async def _apply() -> object:
            return await reconcile_tenant_defaults(
                deps.anthropic,
                deps.sessionmaker,
                deps.defaults_root,
                tenant_id=tenant_id,
                public_url=deps.public_url,
            )

        # --- Resolve agent/environment via ma_resolver (self-heals on
        # archive/recreate); MAResolverMissError propagates unwrapped. This path
        # always passes cached_id=None, so the resolver's own liveness step never
        # runs here, and its short TTL cache can hand back an id for an agent
        # archived since it was cached -- the retrieve below is what settles it. ---
        agent_id = config.responder_ma_agent_id
        if agent_id is None:
            agent_id = await resolve_agent(
                deps.anthropic,
                tenant_id=tenant_id,
                daimon_tag=config.agent_name,
                cached_id=None,
                apply_callable=_apply,
                cache=deps.resolver_cache,
            )
        env_id = await resolve_environment(
            deps.anthropic,
            tenant_id=tenant_id,
            daimon_tag=config.environment_name,
            cached_id=None,
            apply_callable=_apply,
            cache=deps.resolver_cache,
        )

        # A bound thread resolves its responder by concrete id, never by name: a
        # recreated namesake must not inherit the conversation. Which concrete
        # lookup depends on why the thread is bound -- a setup conversation must
        # answer as the built-in Daimon (`get_setup_responder` asserts that), while
        # a thread whose task was handed to another agent answers as that agent,
        # which is an ordinary tenant-and-archive-checked retrieve.
        if config.thread_binding_kind == "handoff":
            agent = await get_setup_agent(deps.anthropic, tenant_id=tenant_id, ma_agent_id=agent_id)
        elif config.thread_binding_id is not None:
            agent = await get_setup_responder(
                deps.anthropic, tenant_id=tenant_id, ma_agent_id=agent_id
            )
        else:
            try:
                agent = await retrieve_agent_cached(
                    deps.anthropic, deps.resolver_cache, tenant_id, agent_id
                )
            except APIStatusError as err:
                if err.status_code not in (400, 404):
                    raise
                deps.resolver_cache.pop((tenant_id, "agent", config.agent_name), None)
                raise MAResolverMissError(
                    kind="agent", tenant_id=tenant_id, daimon_tag=config.agent_name
                ) from err
        if (observation := current_outcome.get()) is not None:
            observation.agent_id = agent.id
        try:
            environment = await retrieve_environment_cached(
                deps.anthropic, deps.resolver_cache, tenant_id, env_id
            )
        except APIStatusError as err:
            if err.status_code not in (400, 404):
                raise
            deps.resolver_cache.pop((tenant_id, "environment", config.environment_name), None)
            raise MAResolverMissError(
                kind="environment", tenant_id=tenant_id, daimon_tag=config.environment_name
            ) from err
    mark("agent_environment")

    # --- Liveness check on the already-retrieved agent: it was archived out of
    # band since the resolver cached/looked up its id. Self-heal the scope
    # rows naming it (tenant-wide) before raising, so the next mention resolves
    # the next cascade tier instead of re-hitting this same dead binding, and
    # raise the existing resolver-miss error so the friendly copy at the four
    # adapter catch sites renders unchanged -- no new error taxonomy. ---
    if agent.archived_at is not None:
        if not selfhosted:
            deps.resolver_cache.pop((tenant_id, "agent", config.agent_name), None)
            deps.resolver_cache.agents.pop((tenant_id, agent_id), None)
            async with deps.sessionmaker() as session, session.begin():
                await clear_agent_references(
                    session, tenant_id=tenant_id, agent_name=config.agent_name
                )
        raise MAResolverMissError(kind="agent", tenant_id=tenant_id, daimon_tag=config.agent_name)
    if environment.archived_at is not None:
        deps.resolver_cache.pop((tenant_id, "environment", config.environment_name), None)
        deps.resolver_cache.environments.pop((tenant_id, env_id), None)
        raise MAResolverMissError(
            kind="environment", tenant_id=tenant_id, daimon_tag=config.environment_name
        )

    # --- Agent pin: an operator can tie an agent to named channels because of
    # what its credentials reach. It runs after the cascade because it depends
    # on which agent answers, and it checks both the cascade's name and the
    # agent's own, so a handed-off thread (resolved by id) is covered too. A DM
    # has no channel, so it is outside every pin. Admins are trusted and exempt
    # only in a DM, where the reply reaches no one else; in a channel or
    # thread other members would see it, so the pin holds for them too. The
    # role is the adapter's live one, never a stored role. ---
    grant = AdmissionGrant(
        tenant_id=tenant_id,
        subject=subject,
        surface=Surface.DM if is_dm else Surface.CHANNEL,
        turn_place=turn_place,
        agent=build_agent_ref(agent.name, agent.metadata, config.agent_name),
        run_place=Place()
        if is_dm
        else replace(
            turn_place,
            setup_thread=config.thread_binding_kind == "setup",
        ),
        channel_id=channel_id,
        thread_id=thread_id,
        is_dm=is_dm,
        hold=hold,
        platform=platform,
    )
    _require_run_agent(policy, grant)
    mark("agent_policy")

    # Every id that seals the turn: its channel, and the thread sealed on its
    # own (a Discord thread by id, a Slack one as channel_id:thread_ts). All of
    # them are recorded, so unsealing one later leaves the others holding.
    seal_ids = limiting_ids_at(policy, channel_id=channel_id, thread_id=thread_id)
    source_sealed = bool(seal_ids)
    memory_read_only = _memory_read_only(policy, grant, source_sealed)

    # The agent's rules on daimon's own tools, read once per turn (no query
    # unless tool safety and agent rules are both on).
    daimon_rules = await daimon_rule_set_for(
        deps.sessionmaker, deps.tool_safety, tenant_id=tenant_id, agent_id=agent.id
    )
    mark("daimon_rule_tools")

    backend_counterpart: AgentRevision | None = None
    backend_agent_revision: AgentBackendRevision | None = None
    source_agent: BackendAgent | None = None

    async def verify_origin(
        selection: ResolvedBackend,
        origin: Literal["channel", "agent"],
        revision: ConfigRevision | None,
        *,
        agent_revision: AgentBackendRevision | None = None,
    ) -> None:
        nonlocal backend_counterpart, backend_agent_revision
        _require_backend_origin(selection, origin, agent_backends=deps.agent_backends)
        if revision is None or selection.profile == MANAGED_AGENTS.profile_id:
            return
        from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
        from daimon.core.turn.ceiling import turn_deadline
        from daimon.core.turn.prepare import (
            ProviderPreparationRequest,
            admitted_session_scope,
        )
        from daimon.core.turn.runtime_manifest import read_preparation_counterpart
        from daimon.core.turn.runtimes import scope_channel_runtimes

        provisional = Admission(
            account_id=principal.account_id,
            agent=agent,
            environment=environment,
            config=config.model_copy(update={"responder_ma_agent_id": agent.id}),
            grant=grant,
            memory_read_only=memory_read_only,
            asks_before_publishing=_asks_before_publishing(policy, grant),
            daimon_rule_tools=daimon_rules.tools,
            daimon_rules_need_driver=daimon_rules.needs_driver,
            source_sealed=source_sealed,
            hold=hold,
            shared_channel=shared,
            origin_channel_id=channel_id,
            origin_thread_id=thread_id,
            origin_seal_ids=seal_ids,
            private_dm_id=(thread_id or channel_id) if is_dm else None,
            channel_skills=channel_skills,
            backend_revision=revision,
            backend_revision_origin=origin,
            backend_agent_revision=agent_revision,
        )
        request = ProviderPreparationRequest(
            deps=deps,
            admission=provisional,
            scope=admitted_session_scope(
                provisional, tenant_id=tenant_id, session_id="preparation"
            ),
            tenant_id=tenant_id,
            platform=platform,
            external_user_id=external_user_id,
            thread_id=thread_id or channel_id,
            session_account_id=principal.account_id,
            reuse_existing=True,
            capabilities=DEFAULT_MA_CAPABILITIES,
            transfer=None,
            deadline=turn_deadline(now=now),
            now=lambda: now,
            source=source_agent,
        )
        request, _ = scope_channel_runtimes(request)
        try:
            prepared = await read_preparation_counterpart(request)
        except AdmissionDenied as error:
            if error.reason == "backend_unsupported":
                _log_backend_unsupported("preparation_counterpart", error)
                if error.cause is None:
                    # The agent's own backend: its runtime can't serve this agent here.
                    raise _backend_refusal(revision, "agent", None) from error
            raise
        backend_counterpart = prepared.counterpart
        backend_agent_revision = agent_revision

    async def resolve_preference() -> None:
        nonlocal backend_revision, backend_ever_shared, backend, backend_revision_origin
        nonlocal backend_footer_profile, source_agent
        if selfhosted:
            return
        if selection_enabled and channel is not None:
            if retained_revision is None and not configured_channel:
                # Reuse the authorized source read; a friendly name cannot carry
                # another resource's backend preference.
                try:
                    source_agent = await channel_backend.load_backend_agent(
                        deps.anthropic,
                        deps.sessionmaker,
                        tenant_id=tenant_id,
                        agent_name=(agent.metadata or {}).get(
                            MA_METADATA_KEY_NAME, config.agent_name
                        )
                        or "",
                        actual_agent=agent,
                    )
                except channel_backend.AgentBackendRefused as error:
                    _log_backend_unsupported("source_agent_lookup", error)
                    raise AdmissionDenied(
                        reason="backend_unsupported", cause="agent_backend"
                    ) from error

                attempts = 0
                enabled_backends = policy.enabled_backends

                async def select(
                    channel: ChannelRef = channel,
                ) -> tuple[ConfigRevision | None, bool, Literal["channel", "agent", "default"]]:
                    nonlocal attempts, enabled_backends
                    attempts += 1
                    # Its own session per attempt: a missed policy fence rolls it back
                    # (retry_fences), so nothing is held while waiting for a writer.
                    async with deps.sessionmaker() as selection_session:
                        selected = await channel_backend.resolve_turn_backend(
                            selection_session,
                            channel,
                            source_agent,
                            enabled=True,
                            validate_origin=(
                                verify_origin if deps.agent_backends else _require_backend_origin
                            ),
                        )
                        if attempts > 1:
                            # It waited out a policy write: check the backend against the
                            # policy that write left, read under the fence, not the one
                            # admission read before it (reauthorize re-checks the rest).
                            enabled_backends = (
                                await load_access_policy(selection_session, tenant_id=tenant_id)
                            ).enabled_backends
                        # Preparation uses another transaction. Publish the immutable
                        # effective revision and its origin before that transaction.
                        await selection_session.commit()
                        return selected

                source: Literal["channel", "agent", "default"] = "agent"
                try:
                    # The shared policy fence is a try-lock: a turn admitted while an
                    # admin's policy write holds it waits (bounded) instead of failing
                    # with an error nobody can act on (smoke 0eee1b8e J7, Ref 8V6AW4).
                    backend_revision, backend_ever_shared, source = await retry_fences(
                        select, busy_error=PolicyBusyError
                    )
                    backend = (
                        check_backend(backend_revision, enabled_backends=enabled_backends)
                        if backend_revision is not None
                        else None
                    )
                except (channel_backend.AgentBackendRefused, BackendUnsupported) as error:
                    _log_backend_unsupported("agent_preference", error)
                    raise _backend_refusal(
                        backend_revision, source, enabled_backends, error
                    ) from error
                backend_revision_origin = source if source != "default" else None
                if backend_revision is not None:
                    backend_footer_profile = backend_revision.profile
            elif retained_revision == 0 and not configured_channel:
                async with deps.sessionmaker() as selection_session:
                    preference = await channel_backend.current_agent_backend(
                        selection_session,
                        tenant_id,
                        derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id),
                    )
                if preference is not None and preference.backend is not None:
                    backend_footer_profile = resolve_default(BackendConfig()).profile

    if not deps.agent_backends:
        await resolve_preference()

    budget_channel_id = agent_permissions(policy, grant.agent.names).budget_channel or (
        dm_source_channel_id if is_dm else channel_id
    )
    # The three independent billing decisions still run in their original
    # order, under READ COMMITTED, but share one checked-out connection.
    async with deps.sessionmaker() as billing_session:
        # --- Per-tenant balance -- independent of Stripe config ---
        if await is_over_balance(
            sessionmaker=deps.sessionmaker, tenant_id=tenant_id, session=billing_session
        ):
            from daimon.core.stores import tenant_ledger

            credited = await tenant_ledger.ever_credited(billing_session, tenant_id=tenant_id)
            raise AdmissionDenied(
                reason="balance_depleted", cause=None if credited else "no_credit_yet"
            )
        mark("balance")

        # --- Monthly usage cap ---
        if await is_over_cap(
            billing_config=deps.billing_config,
            sessionmaker=deps.sessionmaker,
            tenant_id=tenant_id,
            user_id=external_user_id,
            now=now,
            session=billing_session,
        ):
            raise AdmissionDenied(reason="cap_exceeded")
        mark("user_cap")

        # --- Channel budget, including the source of a DM or isolated agent ---
        budget_exceeded = await is_over_channel_budget(
            sessionmaker=deps.sessionmaker,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=budget_channel_id,
            now=now,
            session=billing_session,
        )
        mark("channel_budget")
        channel_skill_rows = (
            []
            if is_dm or budget_exceeded
            else await list_channel_skills(
                billing_session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
            )
        )
    if budget_exceeded:
        if tenant_id not in deps.budget_notices_off:
            spawn_budget_notice(
                sessionmaker=deps.sessionmaker,
                notifier=deps.budget_notifier,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=budget_channel_id,
                now=now,
                server_admins=deps.server_admins,
            )
        raise AdmissionDenied(reason="channel_budget_exceeded")

    if selfhosted:
        # Self-hosted skills are record references, resolved by the harness's
        # mount pipeline. Anthropic's platform/channel catalog is never queried.
        platform_skills: tuple[MaSkill, ...] = ()
        channel_skills: tuple[MaSkill, ...] = ()
    else:
        platform_skills = await turn_platform_skills(
            deps.anthropic,
            tenant_id=tenant_id,
            platform=platform,
            agent=agent,
            platform_skills=deps.platform_skills,
            now=now,
            memory=deps.platform_skill_memory,
            cache=deps.resolver_cache,
        )
        channel_skills = (
            ()
            if is_dm
            else await turn_channel_skills(
                deps.sessionmaker,
                deps.anthropic,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel_id,
                agent=agent,
                agent_names=grant.agent.names,
                rows=channel_skill_rows,
                cache=deps.resolver_cache,
                platform_skills=platform_skills,
            )
        )

    mark("channel_skills")

    if deps.agent_backends:
        await resolve_preference()
    platform_skills = backend_platform_skills(
        platform_skills, None if backend_revision is None else backend_revision.profile
    )
    channel_skills = (*platform_skills, *channel_skills)

    result = Admission(
        backend_revision=backend_revision,
        backend_revision_origin=backend_revision_origin,
        backend_counterpart=backend_counterpart,
        backend_agent_revision=backend_agent_revision,
        backend_footer_profile=backend_footer_profile,
        backend_ever_shared=backend_ever_shared,
        backend=backend,
        memory_read_only=memory_read_only,
        asks_before_publishing=_asks_before_publishing(policy, grant),
        daimon_rule_tools=daimon_rules.tools,
        daimon_rules_need_driver=daimon_rules.needs_driver,
        source_sealed=source_sealed,
        source_shared=source_shared,
        hold=hold,
        shared_channel=shared,
        origin_channel_id=channel_id,
        origin_thread_id=thread_id,
        origin_seal_ids=seal_ids,
        # Every DM-admitted session is private: the transcript tools never open
        # it to anyone but its own execution grant, admins included. /dm-me
        # replaces this with its execution-specific grant.
        private_dm_id=(thread_id or channel_id) if is_dm else None,
        account_id=principal.account_id,
        agent=agent,
        environment=environment,
        config=config.model_copy(update={"responder_ma_agent_id": agent.id}),
        budget_channel_id=budget_channel_id,
        channel_skills=channel_skills,
        platform_skill_ids=frozenset(skill.skill_id for skill in platform_skills),
        grant=grant,
    )
    if released:
        # Admitted: now the release may consume its request (`daimon.core.thread_release`).
        await apply_thread_release(
            deps.sessionmaker,
            state_store,
            own_slot,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            now=now,
        )
    mark("result")
    _log.info(
        "turn.admission_timing",
        tenant_id=str(tenant_id),
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        total_ms=round((perf_counter() - started) * 1000, 1),
        stage_ms=stage_ms,
    )
    return result


async def _record_requester(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    account_id: uuid.UUID,
    requester_org: RequesterOrg,
    requester_home_id: str | None,
    role: Role | None,
    already_foreign: bool,
) -> None:
    """Write what the platform verified about the requester, and nothing it did not.

    HOST: the adapter's live role, if any. OTHER: the foreign marker, and role USER
    whatever the caller passed, so a stored admin is demoted. UNKNOWN: nothing at
    all, so a lookup that failed once never demotes or promotes a stored role. A
    principal already marked foreign is written USER on any verdict but UNKNOWN,
    so a HOST claim never restores an admin role it lost.
    """
    if requester_org is RequesterOrg.OTHER:
        if not requester_home_id:
            raise ValueError("requester_home_id is required when requester_org is OTHER")
        await mark_principal_foreign(session, principal_id=principal_id, home_id=requester_home_id)
        await set_role(session, account_id, Role.USER)
    elif requester_org is RequesterOrg.HOST and role is not None:
        await set_role(session, account_id, Role.USER if already_foreign else role)


async def _record_shared_channel(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    requester_org: RequesterOrg,
    channel_shared: bool | None,
) -> bool:
    """Whether the channel is shared with another organisation, recording it when learned.

    The platform's live signal wins as the recorded source; a verified `OTHER`
    requester also reveals it. An `UNKNOWN` requester never marks a channel.
    """
    source: Literal["platform", "client"] | None = None
    if channel_shared is True:
        source = "platform"
    elif requester_org is RequesterOrg.OTHER:
        source = "client"
    if source is not None:
        await mark_channel_shared(
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            source=source,
        )
        return True
    return await is_channel_shared(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    )


def _require_turn_start(
    policy: TenantAccessPolicy, subject: Subject, place: Place, hold: TurnHold | None
) -> None:
    """Protection, then the invoker allowlist; any denial refuses the turn."""
    decision = authorize(policy, subject=subject, action=Action.START_TURN, place=place, hold=hold)
    if not decision:
        raise AdmissionDenied(
            reason="invoker_not_allowed"
            if decision.reason == "invoker_not_allowed"
            else "writers_none"
        )


def _require_run_agent(policy: TenantAccessPolicy, grant: AdmissionGrant) -> None:
    """The agent pin, then channel isolation; any denial refuses the turn."""
    decision = authorize(
        policy,
        subject=grant.subject,
        action=Action.RUN_AGENT,
        surface=grant.surface,
        agent=grant.agent,
        place=grant.run_place,
        hold=grant.hold,
    )
    if not decision:
        raise AdmissionDenied(
            reason="own_agents_only" if decision.reason == "own_agents_only" else "runs_elsewhere"
        )


def _asks_before_publishing(policy: TenantAccessPolicy, grant: AdmissionGrant) -> bool:
    return not authorize(
        policy,
        subject=grant.subject,
        action=Action.PUBLISH,
        agent=grant.agent,
        origin=grant.run_place,
        hold=grant.hold,
    )


def _is_own_agent(policy: TenantAccessPolicy, grant: AdmissionGrant) -> bool:
    """An isolated channel's own agent at work there, whose memory stays writable."""
    here = channel_permissions(
        policy,
        channel_id=grant.run_place.channel_id,
        parent_channel_id=grant.run_place.parent_channel_id,
    )
    return at_home(agent_permissions(policy, grant.agent.names), here)


async def _tightened_hold(
    session: AsyncSession, admission: Admission, grant: AdmissionGrant
) -> tuple[AdmissionGrant, bool]:
    """The grant and shared flag with what was learned since admission; never relaxed.

    A requester marked foreign, or a channel recorded as shared, since the turn
    was admitted holds it to its channel now, drops its admin standing and makes
    the subject a client. Whatever the admission already held stays held.
    """
    shared = admission.shared_channel
    external_id = grant.subject.platform_user_id
    if grant.platform is None or external_id is None:
        return grant, shared
    principal = await find_platform_principal(
        session, tenant_id=grant.tenant_id, platform=grant.platform, external_id=external_id
    )
    if grant.is_dm:
        # A DM is never held, so what was learned since admission refuses it:
        # its requester is now known to be from another organisation, or the
        # channel it was moved from is now known to be shared.
        if principal is not None and principal.foreign_home_id is not None:
            raise AdmissionDenied(reason="client_requester")
        # A DM moved from a channel is budgeted to it, so that is its source
        # even when the DM path has not attached the source's seal facts.
        source = (
            grant.dm_source.channel_id
            if grant.dm_source is not None
            else admission.budget_channel_id
        )
        if source is not None and await is_channel_shared(
            session, tenant_id=grant.tenant_id, platform=grant.platform, channel_id=source
        ):
            raise DmSourceSealedError("dm_source_shared")
        return grant, shared
    client = grant.subject.client or (
        principal is not None and principal.foreign_home_id is not None
    )
    shared = shared or await is_channel_shared(
        session, tenant_id=grant.tenant_id, platform=grant.platform, channel_id=grant.channel_id
    )
    if not (client or shared):
        return grant, shared
    hold = TurnHold(grant.platform, grant.channel_id, client)
    if grant.hold is not None:
        hold = TurnHold(hold.platform, grant.hold.channel_id, client or grant.hold.client)
    subject = build_subject(
        is_admin=False,
        platform_user_id=external_id,
        via_agent_key=grant.subject.via_agent_key,
        client=hold.client,
    )
    return replace(grant, subject=subject, hold=hold), shared


async def reauthorize(deps: TurnDeps, admission: Admission) -> Admission:
    """Decide an admission again, on the policy as it is now.

    Called at the moment a session is built, reused, replaced or recovered:
    protection, the invoker allowlist and the agent pin are re-asked of
    `authorize` with a fresh policy read, and a turn that would now be
    refused raises `AdmissionDenied` before any session runs it. A seal added
    since admission joins the turn's seal ids (they only grow), so the
    session is stamped with it and memory turns read-only. An unreadable
    policy raises `AccessPolicyUnreadable` -- refused, never open.

    A hand-built admission with no recorded grant (tests only) is returned
    unchanged.
    """
    grant = admission.grant
    if grant is None:
        return admission
    async with deps.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=grant.tenant_id)
        grant, shared_channel = await _tightened_hold(session, admission, grant)
    _require_turn_start(policy, grant.subject, grant.turn_place, grant.hold)
    _require_run_agent(policy, grant)
    if deps.backend_selection_enabled and admission.backend_revision is not None:
        try:
            check_backend(admission.backend_revision, enabled_backends=policy.enabled_backends)
        except BackendUnsupported as error:
            _log_backend_unsupported("reauthorize_policy", error)
            raise _backend_refusal(
                admission.backend_revision,
                admission.backend_revision_origin,
                policy.enabled_backends,
            ) from error
    if grant.dm_source is not None and dm_source_limited(
        policy,
        source_channel_id=grant.dm_source.channel_id,
        source_thread_id=grant.dm_source.thread_id,
        source_thread_keys=grant.dm_source.thread_keys,
    ):
        raise DmSourceSealedError("dm_source_limited")
    seal_ids = admission.origin_seal_ids | limiting_ids_at(
        policy, channel_id=grant.channel_id, thread_id=grant.thread_id
    )
    # Memory posture is decided from the policy as it is now, not only from
    # new seals: a DM turn picks up `dm_memory_read_only` switched on since
    # admission. Read-only never relaxes back to writable here.
    memory_read_only = admission.memory_read_only or _memory_read_only(
        policy, grant, bool(seal_ids)
    )
    asks_before_publishing = _asks_before_publishing(policy, grant)
    # The agent's rules on daimon's own tools, as they are now (one query, and
    # none unless tool safety and agent rules are both on). A rule added or
    # cleared since admission reaches the decider of this turn and a fleet
    # worker prepared from an old root; the session's tools follow at its bind.
    rules = await daimon_rule_set_for(
        deps.sessionmaker, deps.tool_safety, tenant_id=grant.tenant_id, agent_id=admission.agent.id
    )
    if (
        grant == admission.grant
        and shared_channel == admission.shared_channel
        and seal_ids == admission.origin_seal_ids
        and memory_read_only == admission.memory_read_only
        and asks_before_publishing == admission.asks_before_publishing
        and rules.tools == admission.daimon_rule_tools
        and rules.needs_driver == admission.daimon_rules_need_driver
    ):
        return admission
    return replace(
        admission,
        grant=grant,
        hold=grant.hold,
        shared_channel=shared_channel,
        origin_seal_ids=seal_ids,
        source_sealed=admission.source_sealed or bool(seal_ids),
        memory_read_only=memory_read_only,
        asks_before_publishing=asks_before_publishing,
        daimon_rule_tools=rules.tools,
        daimon_rules_need_driver=rules.needs_driver,
    )


async def restrict_inherited_memory(
    deps: TurnDeps, admission: Admission, seals: frozenset[str]
) -> Admission:
    """Historical work retains read-only memory after its channel is unsealed."""
    if not seals or admission.memory_read_only:
        return admission
    grant = admission.grant
    own = False
    if grant is not None:
        async with deps.sessionmaker() as db:
            policy = await load_access_policy(db, tenant_id=grant.tenant_id)
        own_channel = agent_permissions(policy, grant.agent.names).home
        own = _is_own_agent(policy, grant) and all(
            seal
            in {
                own_channel,
                grant.thread_id,
                f"{own_channel}:{grant.thread_id}" if grant.thread_id is not None else None,
            }
            or home_of(policy, seal) == own_channel
            for seal in seals
        )
    return admission if own else replace(admission, memory_read_only=True)


def decide_before_send(deps: TurnDeps, admission: Admission) -> Callable[[], Awaitable[None]]:
    """A last decision to run right before a message is sent into a session.

    Opening the stream and building the message await network calls after the
    session's last decision. A pin or protection added since refuses the send
    (`AdmissionDenied`); a seal or read-only change the session wasn't built
    with makes the turn wait (`SessionBusyError`) so the next one is prepared
    with it, rather than writing into a session mounted for the old decision.
    """

    async def decide() -> None:
        current = await reauthorize(deps, admission)
        if (
            current.origin_seal_ids != admission.origin_seal_ids
            or current.memory_read_only != admission.memory_read_only
            or current.hold != admission.hold
            or current.shared_channel != admission.shared_channel
            or (current.asks_before_publishing and not admission.asks_before_publishing)
        ):
            raise SessionBusyError(
                pending_reasons=("seal",), retry_after=datetime.now(UTC) + timedelta(seconds=1)
            )

    return decide


def _memory_read_only(policy: TenantAccessPolicy, grant: AdmissionGrant, sealed: bool) -> bool:
    return (sealed and not _is_own_agent(policy, grant)) or (
        grant.is_dm and policy.dm_memory_read_only
    )


async def _awaits_its_switch_here(
    deps: TurnDeps,
    source: BackendAgent,
    context: ScopeContext,
    *,
    responder_ma_agent_id: str | None,
) -> bool:
    """`source` is a copy fork_agent made of this place's responder, in this place, still
    on the responder's provider: its confirmed switch hands it this place (J1). The MCP
    tool decides the same (`copy_awaiting_its_switch`); a hand-off to it already made
    reads it as this place's responder before this is asked."""
    from daimon.core.defaults.metadata import (
        MA_METADATA_KEY_FORKED_FROM,
        MA_METADATA_KEY_FORKED_IN,
        forked_in,
    )
    from daimon.core.ma_identity import derive_agent_uuid
    from mux.contracts.config import DEFAULT_BACKEND

    stamp = (source.source.spec.metadata if source.source is not None else None) or {}
    if (
        responder_ma_agent_id is None
        or stamp.get(MA_METADATA_KEY_FORKED_FROM) != responder_ma_agent_id
        or stamp.get(MA_METADATA_KEY_FORKED_IN)
        != forked_in(context.platform, context.channel_id, context.thread_id)
    ):
        return False
    async with deps.sessionmaker() as db:
        copy, responder = (
            (revision.backend if revision is not None else None) or DEFAULT_BACKEND
            for revision in [
                await agent_backends.current(db, context.tenant_id, agent)
                for agent in (
                    source.agent_id,
                    derive_agent_uuid(
                        tenant_id=context.tenant_id, ma_agent_id=responder_ma_agent_id
                    ),
                )
            ]
        )
    return copy == responder


async def capture_backend_source(
    deps: TurnDeps,
    source: BackendAgent,
    *,
    context: ScopeContext,
    subject: Subject,
    place: Place,
    is_dm: bool,
    slack_turn_context_id: uuid.UUID | None,
    private_dm_id: str | None,
    external_user_id: str | None = None,
    explicit_target_name: str | None = None,
    responder_ma_agent_id: str | None = None,
) -> BackendAgent:
    """Read the actual target's source in a verified host context, without admitting a turn.

    `responder_ma_agent_id` is the verified turn's responder, for a copy of it made here.
    """
    from daimon.core.defaults.ma_index import find_environment_by_daimon_tag
    from daimon.core.mux_backend import resource_scope
    from daimon.core.mux_compat import retrieve_agent
    from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from daimon.core.turn.runtime_manifest import capture_preparation_source

    try:
        async with asyncio.timeout(30):
            if context.tenant_id != source.tenant_id or context.account_id is None:
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="identity")
            if source.source is None:
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="source")
            async with deps.sessionmaker() as db:
                account = await get_account_with_tenant(db, account_id=context.account_id)
                policy = await load_access_policy(db, tenant_id=context.tenant_id)
                config = await resolve_config(db, context=context, default=deps.deployment_default)
            if account is None or account.tenant_id != context.tenant_id:
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="identity")
            if (
                subject.platform_user_id is not None
                and subject.platform_user_id != account.platform_user_id
            ):
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="identity")
            if explicit_target_name is not None:
                if (
                    context.platform != "cli"
                    or context.channel_id is not None
                    or context.thread_id is not None
                    or is_dm
                    or source.name != explicit_target_name
                ):
                    raise channel_backend.AgentBackendRefused(
                        "source_unavailable", detail="identity"
                    )
                config = config.model_copy(
                    update={
                        "configuration_target_name": explicit_target_name,
                        "configuration_target_ma_agent_id": source.source.ref.id,
                    }
                )
            if config.configuration_target_name is not None:
                target_matches = (
                    config.configuration_target_name == source.name
                    and config.configuration_target_ma_agent_id == source.source.ref.id
                )
            else:
                target_matches = (
                    config.agent_name == source.name
                    and config.responder_ma_agent_id
                    in (
                        None,
                        source.source.ref.id,
                    )
                )
            if (
                not target_matches
                and explicit_target_name is None
                and config.configuration_target_name is None
                and await _awaits_its_switch_here(
                    deps,
                    source,
                    context,
                    responder_ma_agent_id=config.responder_ma_agent_id or responder_ma_agent_id,
                )
            ):
                # J1/J2: a new copy of this place's responder, made here. Its confirmed
                # switch hands it this place too, so it reads as it will answer here:
                # this place's template and MCP, not a bare agent read.
                config = config.model_copy(
                    update={
                        "agent_name": source.name,
                        "responder_ma_agent_id": source.source.ref.id,
                    }
                )
                target_matches = True
            if not target_matches and explicit_target_name is None:
                # An agent-level setting (provider, model) names any agent the requester
                # may edit, e.g. a copy that isn't this place's responder: read that
                # agent's own source rather than refusing every place but its home.
                return await capture_agent_source(
                    deps, source, account_id=context.account_id, subject=subject
                )
            if not target_matches or config.environment_name is None:
                raise channel_backend.AgentBackendRefused(
                    "source_unavailable", detail="environment"
                )
            label = external_user_id or account.platform_user_id
            if not label:
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="identity")
            scope = resource_scope(
                tenant_id=str(context.tenant_id),
                account_id=str(context.account_id),
                authorization_id="backend-preview",
            )
            actual = await retrieve_agent(deps.anthropic, source.source.ref.id, scope=scope)
            if explicit_target_name is not None:
                await channel_backend.load_backend_agent(
                    deps.anthropic,
                    deps.sessionmaker,
                    tenant_id=context.tenant_id,
                    agent_name=explicit_target_name,
                    actual_agent=actual,
                )
            environment = await find_environment_by_daimon_tag(
                deps.anthropic,
                tenant_id=context.tenant_id,
                name=config.environment_name,
            )
            if environment is None or environment.archived_at is not None:
                raise channel_backend.AgentBackendRefused(
                    "source_unavailable", detail="environment"
                )
            grant = AdmissionGrant(
                tenant_id=context.tenant_id,
                subject=subject,
                surface=Surface.DM if is_dm else Surface.CHANNEL,
                turn_place=place,
                run_place=Place() if is_dm else place,
                agent=build_agent_ref(actual.name, actual.metadata or {}, source.name),
                channel_id=context.channel_id or "",
                thread_id=context.thread_id,
                is_dm=is_dm,
            )
            if (
                place.category_unresolved
                or place.parent_unresolved
                or (
                    context.channel_id is not None
                    and (place.channel_id, place.parent_channel_id)
                    != (context.thread_id or context.channel_id, context.channel_id)
                )
            ):
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="place")
            seals = limiting_ids_at(policy, channel_id=grant.channel_id, thread_id=grant.thread_id)
            skills = (
                await turn_channel_skills(
                    deps.sessionmaker,
                    deps.anthropic,
                    tenant_id=context.tenant_id,
                    platform=context.platform or account.platform,
                    channel_id=grant.channel_id,
                    agent=actual,
                    agent_names=grant.agent.names,
                )
                if context.channel_id is not None
                else ()
            )
            rule_set = await daimon_rule_set_for(
                deps.sessionmaker,
                deps.tool_safety,
                tenant_id=context.tenant_id,
                agent_id=actual.id,
            )
            admission = Admission(
                context.account_id,
                actual,
                environment,
                config,
                grant=grant,
                memory_read_only=_memory_read_only(policy, grant, bool(seals)),
                asks_before_publishing=_asks_before_publishing(policy, grant),
                daimon_rule_tools=rule_set.tools,
                daimon_rules_need_driver=rule_set.needs_driver,
                source_sealed=bool(seals),
                origin_channel_id=context.channel_id,
                origin_thread_id=context.thread_id,
                origin_seal_ids=seals,
                channel_skills=skills,
                slack_turn_context_id=slack_turn_context_id,
                private_dm_id=(private_dm_id or context.thread_id or context.channel_id)
                if is_dm
                else private_dm_id,
            )
            request = ProviderPreparationRequest(
                deps,
                admission,
                scope,
                context.tenant_id,
                context.platform or account.platform,
                label,
                context.thread_id or "",
                context.account_id,
                False,
                DEFAULT_MA_CAPABILITIES,
                None,
                datetime.now(UTC) + timedelta(seconds=30),
                lambda: datetime.now(UTC),
            )
            captured = await capture_preparation_source(
                request, fresh=True, capability_preview=True
            )
            if (captured.tenant_id, captured.agent_id) != (source.tenant_id, source.agent_id):
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="source")
            return captured
    except (APIError, TimeoutError) as error:
        raise channel_backend.AgentBackendRefused("source_unavailable") from error
    except ScopeViolation as error:
        raise channel_backend.AgentBackendRefused("source_unavailable", detail="place") from error


async def prepare_worker_agent(
    deps: TurnDeps,
    root: ProviderPreparationRequest,
    source: BackendAgent,
) -> ProviderPreparationRequest:
    """The preparation request for a fleet worker that runs AS another on-server agent.

    Built from the root's own preparation request: the same requester, run place,
    thread, scope and environment, with the agent replaced and every agent-specific
    admission fact decided again for that agent (memory, publishing, Daimon tool
    rules, channel skills). `reauthorize` then asks the current policy whether this
    requester may run that agent here; a refusal raises before any source read.
    Agent backend facts are cleared: the worker's backend is resolved later from its
    own worker grant. The captured source is that agent's, never the root's.
    """
    from daimon.core.mux_compat import retrieve_agent
    from daimon.core.turn.runtime_manifest import capture_preparation_source

    grant = root.admission.grant
    if (
        grant is None
        or root.worker is not None
        or source.source is None
        or source.tenant_id != root.tenant_id
        or grant.is_dm
    ):
        raise channel_backend.AgentBackendRefused("source_unavailable")
    actual = await retrieve_agent(deps.anthropic, source.source.ref.id, scope=root.scope)
    worker_grant = replace(
        grant, agent=build_agent_ref(actual.name, actual.metadata or {}, source.name)
    )
    async with deps.sessionmaker() as db:
        policy = await load_access_policy(db, tenant_id=root.tenant_id)
    seals = root.admission.origin_seal_ids
    rule_set = await daimon_rule_set_for(
        deps.sessionmaker, deps.tool_safety, tenant_id=root.tenant_id, agent_id=actual.id
    )
    skills = (
        await turn_channel_skills(
            deps.sessionmaker,
            deps.anthropic,
            tenant_id=root.tenant_id,
            platform=root.platform,
            channel_id=worker_grant.channel_id,
            agent=actual,
            agent_names=worker_grant.agent.names,
        )
        if worker_grant.channel_id
        else ()
    )
    admission = replace(
        root.admission,
        agent=actual,
        config=root.admission.config.model_copy(
            update={
                "agent_name": source.name,
                "responder_ma_agent_id": actual.id,
                "configuration_target_ma_agent_id": None,
                "configuration_target_name": None,
            }
        ),
        grant=worker_grant,
        # The root's read-only memory carries over (e.g. seals it inherited earlier
        # that are not in origin_seal_ids), so a worker never writes sealed context
        # into that agent's shared memory; the agent's own rule can only add to it.
        memory_read_only=root.admission.memory_read_only
        or _memory_read_only(policy, worker_grant, bool(seals)),
        asks_before_publishing=_asks_before_publishing(policy, worker_grant),
        daimon_rule_tools=rule_set.tools,
        daimon_rules_need_driver=rule_set.needs_driver,
        channel_skills=skills,
        backend_revision=None,
        backend=None,
        backend_revision_origin=None,
        backend_footer_profile=None,
        backend_counterpart=None,
        backend_agent_revision=None,
    )
    admission = await reauthorize(deps, admission)
    request = replace(
        root,
        admission=admission,
        source=None,
        counterpart=None,
        verified_counterpart=None,
        target_spec=None,
    )
    captured = await capture_preparation_source(request)
    if (captured.tenant_id, captured.agent_id) != (source.tenant_id, source.agent_id):
        raise channel_backend.AgentBackendRefused("source_unavailable")
    return replace(request, source=captured)


async def capture_agent_source(
    deps: TurnDeps,
    source: BackendAgent,
    *,
    account_id: uuid.UUID,
    subject: Subject,
) -> BackendAgent:
    """Capture the named agent's own source: the requester's tenant and account, no channel.

    The agent-scoped form of `capture_backend_source`, for settings that belong to the
    agent wherever it answers (fleet config, provider and model).
    """
    return await capture_backend_source(
        deps,
        source,
        context=ScopeContext(tenant_id=source.tenant_id, account_id=account_id, platform="cli"),
        subject=subject,
        place=Place(),
        is_dm=False,
        slack_turn_context_id=None,
        private_dm_id=None,
        explicit_target_name=source.name,
    )


async def capture_admitted_backend_source(
    deps: TurnDeps,
    source: BackendAgent,
    *,
    admission: Admission | None,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
) -> BackendAgent:
    """A chat confirmation card's source capture: only its own turn's public admission."""
    from daimon.core.session_seal import is_private_conversation

    if admission is None or admission.grant is None or admission.account_id != account_id:
        raise channel_backend.AgentBackendRefused("source_unavailable")
    # Like the MCP caller's public-context gate: never a DM or private conversation.
    if (
        admission.grant.is_dm
        or admission.private_dm_id is not None
        or admission.slack_turn_context_id is not None
        or admission.origin_channel_id is None
        or is_private_conversation(admission.origin_channel_id)
        or is_private_conversation(admission.origin_thread_id or "")
    ):
        raise channel_backend.AgentBackendRefused("source_context_unavailable")
    return await capture_backend_source(
        deps,
        source,
        context=ScopeContext(
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            channel_id=admission.origin_channel_id,
            thread_id=admission.origin_thread_id,
        ),
        subject=admission.grant.subject,
        place=admission.grant.run_place,
        is_dm=False,
        slack_turn_context_id=None,
        private_dm_id=None,
        responder_ma_agent_id=admission.agent.id,
    )

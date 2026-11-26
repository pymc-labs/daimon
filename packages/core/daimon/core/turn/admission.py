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
from functools import partial
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, Literal, cast

import structlog
from anthropic import APIError, APIStatusError
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core import channel_backend
from daimon.core.access_policy import (
    TenantAccessPolicy,
    agent_invoker_roles,
    with_public_thread_room,
)
from daimon.core.agent_identity import shown_name
from daimon.core.agent_record_ports import DAIMON_ID_PREFIXES
from daimon.core.anthropic_guard import anthropic_configured
from daimon.core.approval_gate import ApprovalGate, resolve_approval_gate
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
    holds_agent_roles,
)
from daimon.core.channel_audience import (
    AudienceMode,
    ChannelAudience,
    audience_mode,
    channel_has_outsiders,
    log_shadow,
    mode_of,
    requester_standing,
)
from daimon.core.channel_backend import (
    BackendUnsupported,
    backend_is_enabled,
    channel_ref,
    check_backend,
    current_backend_and_sharing,
)
from daimon.core.channel_backend import log_backend_unsupported as _log_backend_unsupported
from daimon.core.channel_budget_notice import spawn_budget_notice
from daimon.core.channel_skills import turn_channel_skills, turn_platform_skills
from daimon.core.config import env_admits
from daimon.core.data_reading import AFTER_DATA_REFUSAL, AGENT_REQUEST_REFUSAL, reads_data
from daimon.core.data_tools import DATA_READS, DATA_SOURCES
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.errors import TurnError, UserFacingError
from daimon.core.features.model_input import (
    CHAT_KEYS,
    HIDING_KEYS,
    HISTORY_KEYS,
    LIMIT_KEYS,
    PER_TURN_KEYS,
    SESSION_KEYS,
    TOOL_KEYS,
    TURN_KEYS,
    DmHistoryCap,
    HistoryDepth,
    SessionToolset,
    agent_toolset,
    answer_message_cap,
    builtins_of,
    turn_limit_minutes,
)
from daimon.core.features.registry import FEATURES
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.session_identity import IDENTITY_KEYS, SessionIdentity
from daimon.core.features.snapshot import (
    FeaturePlace,
    FeatureSnapshot,
    feature_in,
    place_feature,
    resolve_features,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import (
    MAResolverMissError,
    resolve_agent,
    resolve_environment,
    retrieve_agent_cached,
    retrieve_environment_cached,
)
from daimon.core.membership_subject import resolve_subject, resolve_subject_authority
from daimon.core.permissions import (
    agent_permissions,
    at_home,
    channel_permissions,
    dm_source_limited,
    home_of,
    limiting_ids_at,
)
from daimon.core.ports.data_ready import has_sources
from daimon.core.ports.gate import data_sources_on
from daimon.core.relay_scope import (
    agent_has_identity_relay,
    identity_relay_server_names,
    relay_seal_ids,
    without_data_reads,
    without_data_sources,
    without_identity_relays,
    without_long_term_memory,
)
from daimon.core.scope import HelperTarget, ResolvedConfig, ScopeContext
from daimon.core.session_fence_retry import retry_fences
from daimon.core.session_snapshot import MaSkill
from daimon.core.setup_conversations import get_setup_agent, get_setup_responder
from daimon.core.skill_audience import TurnPlace, scoped_skill_audience
from daimon.core.spend_gate import (
    AFTER_QUEUE_CHECKS,
    SpendPerson,
    SpendRequest,
    spend_gate,
    spend_gate_in_new_session,
)
from daimon.core.stores import agent_backends, mux_state
from daimon.core.stores.access_policy import PolicyBusyError, load_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.core.stores.data_memory import tenant_has_reads
from daimon.core.stores.domain import AgentBackendRevision, BackendAgent, Role
from daimon.core.stores.identity import (
    find_platform_principal,
    get_or_create_platform_principal,
    mark_principal_foreign,
)
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from daimon.core.stores.scoped_config_write import clear_agent_references
from daimon.core.stores.shared_channels import mark_channel_shared
from daimon.core.stores.thread_sessions import get_live_thread_session, get_thread_session_at
from daimon.core.thread_models import apply_thread_model, conversation_key, thread_backend
from daimon.core.thread_release import (
    apply_thread_release,
    release_due,
    release_now,
    release_pending,
)
from daimon.core.turn.approvals import daimon_rule_tools_for
from daimon.core.turn.ceiling import limit_ceiling_s
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import (
    AdmissionDenialReason,
    AdmissionDenied,
    BackendRefusalCause,
    DmSourceSealedError,
    MissingTurnConfigError,
    SessionBusyError,
)
from daimon.core.turn.invocation import unattended_invocation
from daimon.core.turn.outcomes import TurnObservation, backend_label, current_outcome
from daimon.core.turn.slots import register_spend_recheck
from daimon.core.turn.stage_timing import elapsed_ms, stage_timer
from daimon.core.turn_hold import RequesterOrg, TurnHold
from daimon.core.web_links import TenantLinks
from daimon.core.web_roles_rules import is_admin as is_web_admin
from daimon.core.web_roles_rules import role_of_account
from mux.contracts.admission import Admission as BackendAdmission
from mux.contracts.config import BackendConfig, ConfigRevision, ResolvedBackend, resolve_default
from mux.contracts.ids import ChannelRef, Provider, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import ScopeViolation
from mux.profiles import INLINE_REUSE, MANAGED_AGENTS, PERSISTENT_WORKSPACE
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
    "StandingRequest",
    "reauthorize",
]


if TYPE_CHECKING:
    pass

_log = structlog.get_logger(__name__)

#: Seconds a switch preview may spend reading its source. A cold agent with 23 custom
#: skills at `latest` needs about 69 Skills API requests (a version lookup, a version
#: list and a download per skill); the process paces them at 0.75 s, about 52 s.
PREVIEW_CAPTURE_BUDGET_S = 90


@dataclass(frozen=True)
class DmSource:
    """Where a DM conversation was started from, re-checked by `reauthorize`."""

    channel_id: str | None
    thread_id: str | None
    thread_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class StandingRequest:
    """Ask `admit` to work out a Discord requester's standing inside its own transaction.

    `event_role_ids` are the roles the event carried (None when it carried none); the
    member mirror and one fresh fetch (`deps.membership_resolver`) follow, in that order.
    The result replaces the `requester_org` passed in.
    """

    event_role_ids: tuple[str, ...] | None = None


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
    cold_dm: bool = False
    act_for_creator: bool = False
    human_invocation: bool = False
    # Set by the DM path: the conversation's source, whose seal is re-checked.
    dm_source: DmSource | None = None
    # The channel the turn is held to; every question asked again carries it, so
    # a hold never relaxes between admission and the moment the session is built.
    hold: TurnHold | None = None
    # The platform admitted on; `reauthorize` rereads the requester's foreign
    # marker and the channel's shared record with it. None only in hand-built grants.
    platform: str | None = None
    # A DM that has no source by design (a private answer): the agent's budget channel
    # is its spend channel only, never a source whose seal could refuse it. False: the
    # source was not recorded, and the budget channel stands in for it.
    dm_no_source: bool = False
    # The room is narrow by its stored facts (mode `on`): its memory is read-only, the
    # agent pinned to it included.
    narrow_room: bool = False
    # The platform said `thread_id` is a public thread, so a thread with no room fact yet
    # is judged by its parent (`with_public_thread_room`).
    public_thread: bool = False


@dataclass(frozen=True)
class ThreadFollow:
    """How this conversation followed its agent's model-provider switch (M03)."""

    kind: Literal["moved", "stays"]
    #: The agent's current selection: what a new conversation runs on.
    provider: Provider
    model: str | None


def _effective_model(provider: Provider, model: str | None) -> str | None:
    """A provider default stored as None is that provider's default model."""
    from mux.profiles import backend_selection

    return model or backend_selection(provider).model


async def _thread_follow(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: ChannelRef,
    agent_ma_id: str,
    bound_provider: Provider,
    bound_model: str | None,
    shared: bool,
    enabled_backends: tuple[Provider, ...],
    thread: channel_backend.ThreadBackend | None = None,
    retained_local: int | None = None,
) -> ThreadFollow | None:
    """Whether this conversation must follow its agent's switch (M03).

    The person's own slot of a conversation keeps the provider it started on until
    something moves it. A confirmed switch from chat moves it (`_move_this_thread`);
    every other switch (set_agent_backend, a setup panel, the CLI, a hand-over) used
    to leave it on the old provider while the agent said otherwise. So when the
    answering agent's current selection differs from the provider (or, off
    Anthropic, the effective model) this slot runs on, and that selection can run
    here, the slot moves on this message. `agent_ma_id` is the Anthropic source
    agent admission resolved, never a provider's own agent id. A shared
    conversation never moves ("stays"). Nothing is written here.

    `thread`, the conversation's own model choice, stands in for the agent's.
    A slot bound by an earlier choice (`retained_local`) that no longer holds
    (cleared, changed, or its consent replaced) moves even to the same model.
    """
    from daimon.core.ma_identity import derive_agent_uuid
    from mux.profiles import backend_selection

    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_ma_id)
    preference = (
        thread.consent
        if thread is not None
        else await channel_backend.current_agent_backend(session, tenant_id, agent_id)
    )
    provider: Provider = (
        preference.backend
        if preference is not None and preference.backend is not None
        else MANAGED_AGENTS.provider
    )
    if provider == LOOP.provider:
        return None
    model = _effective_model(provider, preference.model if preference is not None else None)
    pin = (
        await mux_state.config_thread_pin(session, channel, retained_local)
        if retained_local
        else None
    )
    still_chosen = pin is None or (
        thread is not None
        and thread.consent is not None
        and pin == (thread.thread_id, thread.consent.digest)
    )
    if (
        still_chosen
        and bound_provider == provider
        and (
            provider == MANAGED_AGENTS.provider
            or _effective_model(bound_provider, bound_model) == model
        )
    ):
        return None
    # Only toward a provider that can run here: an unavailable preference never
    # strands a conversation that still works where it is.
    selection = backend_selection(provider)
    if model is not None:
        selection = selection.model_copy(update={"model": model})
    try:
        check_backend(
            ConfigRevision.create(channel, 0, selection), enabled_backends=enabled_backends
        )
    except BackendUnsupported:
        return None
    return ThreadFollow("stays" if shared else "moved", provider, model)


@dataclass(frozen=True)
class HelperCall:
    """A helper's turn: the agent it runs as, and its own conversation's thread key.

    The turn is admitted in the asking turn's channel and thread, so every place
    rule, seal and hold applies as there; `thread_key` keys only its own session
    and model-provider slot (`daimon.core.helpers.helper_thread_key`).
    """

    target: HelperTarget
    thread_key: str
    # The asking turn is a relay agent's: its helper inherits the held, sealed,
    # memory-free turn whatever servers the helper itself has (`daimon.core.relay_scope`).
    relay_scoped: bool = False
    # The asking turn's agent reads data sources here (`daimon.core.data_reading`).
    data_reading: bool = False


@dataclass(frozen=True)
class AdmitRequest:
    """What a turn was admitted on, so a helper's turn is admitted on the same."""

    tenant_id: uuid.UUID
    platform: str
    external_user_id: str
    channel_id: str
    thread_id: str | None
    role: Role | None
    role_ids: tuple[str, ...]
    verified_membership: bool | None
    is_dm: bool
    dm_source_channel_id: str | None
    category_id: str | None
    category_unresolved: bool
    requester_org: RequesterOrg
    requester_home_id: str | None
    channel_shared: bool | None
    origin: str
    attended: bool | None
    spend_person: SpendPerson | None
    surface: Surface | None
    principal_platform: str | None
    public_thread: bool = False
    # The turn was admitted as a relay agent's (`daimon.core.relay_scope`); its helpers inherit it.
    relay_scoped: bool = False
    data_reading: bool = False


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
    # The decider changes; the provider session keeps the same approval pauses.
    approval_gate: ApprovalGate | None = field(default=None, compare=False)
    # Daimon tools this agent has an ask or deny rule on, read when the turn is
    # admitted (empty while rules are off): their session tools wait for the
    # driver's decision, so the set is part of the tools the session is bound to.
    daimon_rule_tools: frozenset[str] = frozenset()
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
    # A relay agent's turn (`daimon.core.relay_scope`): held, sealed, no memory, a fresh session.
    relay_scoped: bool = False
    # The turn's agent reads data sources here (`daimon.core.data_reading`): its provider
    # memory mounts read-only (`stores_read_only`) and writes out ask first.
    data_reading: bool = False
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
    # Scoped skills the agent or channel carries that this turn's audience may not read, so
    # the session runs without them (`daimon.core.skill_audience`); None when not checked,
    # and the session then leaves out every scoped skill of the agent's.
    withheld_skills: frozenset[str] | None = None
    # What admission was decided on; `reauthorize` decides it again at the
    # moment the session is built. None only for hand-built test admissions.
    grant: AdmissionGrant | None = field(default=None, compare=False, repr=False)
    is_platform_admin: bool = False
    effective_is_admin: bool | None = None
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
    # M03: this conversation moved to the agent's switched model provider on this
    # message ("moved"), or is shared and stays on its own ("stays", with the
    # agent's selection for new conversations).
    backend_follow: ThreadFollow | None = field(default=None, compare=False, repr=False)
    backend_counterpart: AgentRevision | None = field(default=None, compare=False, repr=False)
    backend_agent_revision: AgentBackendRevision | None = field(
        default=None, compare=False, repr=False
    )
    # The conversation whose own model choice selected `backend_revision`; its
    # consent is `backend_agent_revision`. None when the agent's own choice did.
    backend_thread_id: str | None = field(default=None, compare=False, repr=False)
    # The model this conversation's people chose (`daimon.core.thread_models`),
    # already in `agent`; the session is created with it. None runs the agent's own.
    model_override: str | None = None
    # The effort that session runs at when the people chose one; None keeps the agent's.
    model_effort: str | None = None
    # A choice was kept but can't run here, so this turn runs the agent's own
    # model and says so.
    model_fell_back: bool = False
    # What `admit` was called with; a helper's turn is admitted on the same.
    request: AdmitRequest | None = field(default=None, compare=False, repr=False)
    # This workspace's links into the web app for the admitted role; None with no web app.
    web: TenantLinks | None = field(default=None, compare=False, repr=False)
    # The turn-bound, session-tool and session-identity features at this place; None only
    # for hand-built admissions, which render every turn control and keep every tool.
    features: FeatureSnapshot | None = field(default=None, compare=False, repr=False)
    # A routine fire whose admin opt-in matched this run keeps its built-in tools.
    routine_sandbox_tools: bool = field(default=False, compare=False, repr=False)

    @property
    def stores_read_only(self) -> bool:
        """Provider memory stores mount read-only: as `memory_read_only` says, or the agent
        reads data, whose memories only Daimon's own memory tags (`daimon.core.data_reading`)."""
        return self.memory_read_only or self.data_reading

    @property
    def toolset(self) -> SessionToolset:
        """What this place's features change in the agent's session tools; a session that
        read data keeps `data_reads` (`data_reading`)."""
        toolset = agent_toolset(self.features, self.agent)
        return replace(toolset, data_reads=True) if self.data_reading else toolset

    @property
    def identity(self) -> SessionIdentity:
        """What a session created now at this place is built with."""
        return SessionIdentity.of(self.features)

    @property
    def history_depth(self) -> HistoryDepth:
        """How much earlier chat a context builder reads at this place."""
        return HistoryDepth.of(self.features)

    @property
    def dm_history(self) -> DmHistoryCap:
        """How much of a DM conversation a reply at this place carries."""
        return DmHistoryCap.of(self.features)

    @property
    def answer_message_cap(self) -> int:
        """The most messages one answer may post here before it is attached as a file."""
        return answer_message_cap(self.features)

    @property
    def turn_ceiling_s(self) -> float:
        """How long a turn here may run before it is stopped, never past
        `LONGEST_TURN_CEILING_S`: a person's message reads the turn time limit set at this
        place, a turn nobody waits on (`unattended_invocation`) the unattended run limit, and a
        hand-built admission or a missing setting the built-in 45 minutes. A turn allowed
        past 45 minutes stores its deadline for the stale-turn and lock-wait checks."""
        unattended = self.request is not None and unattended_invocation(
            self.request.origin, self.request.attended
        )
        return limit_ceiling_s(turn_limit_minutes(self.features, unattended=unattended))

    def feature_on(self, key: str) -> bool:
        """`key` at this place; a hand-built admission, or a key not resolved, keeps it on."""
        found = self.features.features.get(key) if self.features is not None else None
        return found is None or found.on

    @property
    def audience_mode(self) -> AudienceMode | None:
        """The `audience_hold` mode this turn was admitted under; None for a hand-built
        admission, whose caller must read it."""
        found = self.resolved("audience_hold")
        return None if found is None else mode_of(found.value)

    def resolved(self, key: str) -> ResolvedFeature | None:
        """`key` as resolved at this place; None for a hand-built admission or an unresolved key."""
        return self.features.features.get(key) if self.features is not None else None

    def shown(self, key: str, platform: str = "") -> bool:
        """A display feature at this place; a hand-built admission, or a key not resolved,
        keeps the registry default on `platform` (several are off by default, unlike
        `feature_on`)."""
        found = self.features.features.get(key) if self.features is not None else None
        if found is not None:
            return found.on
        return FEATURES[key].default_for(platform) not in ("off", "hidden")

    def display_name(self, fallback: str) -> str:
        """The agent's name as people see it: its `display_name` setting, else `fallback`."""
        return shown_name(self.resolved("display_name"), fallback)

    @property
    def is_admin(self) -> bool:
        """The effective authority admitted for this turn; absent evidence denies."""
        if self.effective_is_admin is not None:
            return self.effective_is_admin
        return self.grant is not None and self.grant.subject.is_admin

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


def _name_backend(revision: ConfigRevision | None) -> None:
    if (observation := current_outcome.get()) is not None:
        observation.backend = backend_label(revision)


def _refuse_managed_fallback(
    *, daimon_record: bool, revision: AgentBackendRevision | ConfigRevision | None
) -> None:
    """A Daimon record never runs on Managed Agents: refuse rather than fall back."""
    if not daimon_record or (revision is not None and revision.backend != MANAGED_AGENTS.provider):
        return
    raise AdmissionDenied(
        reason="backend_unsupported",
        cause="agent_backend",
        provider=MANAGED_AGENTS.provider if revision is None else revision.backend,
    )


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
    role_ids: tuple[str, ...] = (),
    verified_membership: bool | None = None,
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    dm_agent_name: str | None = None,
    routine_agent_name: str | None = None,
    routine_thread_id: str | None = None,
    dm_no_source: bool = False,
    cold_dm: bool = False,
    category_id: str | None = None,
    category_unresolved: bool = False,
    observer: AdmissionObserver | None = None,
    requester_org: RequesterOrg,
    standing: StandingRequest | None = None,
    requester_home_id: str | None = None,
    channel_shared: bool | None = None,
    origin: str,
    attended: bool | None = None,
    spend_person: SpendPerson | None = None,
    surface: Surface | None = None,
    principal_platform: str | None = None,
    helper: HelperCall | None = None,
    public_thread: bool = False,
) -> Admission:
    """Admit a turn, or raise the typed reason it is refused.

    `requester_org` is the platform's word on which organisation the requester
    belongs to; every caller states it. `requester_home_id` names the home
    organisation of an `OTHER` requester. `channel_shared` is the platform's live
    signal that the channel is shared with another organisation (None: no signal).
    A client (another organisation, unknown, or one already recorded as foreign) is
    held to the channel it asked in, as is anyone in a shared channel.
    `surface` overrides the DM-or-channel surface (the web chat), and
    `principal_platform` names the platform `external_user_id` belongs to when it
    is not the one the turn runs on (a web person linked to a platform account).
    `helper` admits a helper's turn as its agent (`admit_helper`), and
    `routine_agent_name` a routine's fire as the routine's own agent, and
    `routine_thread_id` the thread it posts to, whose kept provider it runs on.
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
                role_ids=role_ids,
                verified_membership=verified_membership,
                is_dm=is_dm,
                dm_source_channel_id=dm_source_channel_id,
                dm_agent_name=dm_agent_name,
                routine_agent_name=routine_agent_name,
                routine_thread_id=routine_thread_id,
                dm_no_source=dm_no_source,
                cold_dm=cold_dm,
                category_id=category_id,
                category_unresolved=category_unresolved,
                requester_org=requester_org,
                standing=standing,
                requester_home_id=requester_home_id,
                channel_shared=channel_shared,
                origin=origin,
                attended=attended,
                spend_person=spend_person,
                surface=surface,
                principal_platform=principal_platform,
                helper=helper,
                public_thread=public_thread,
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
    request = AdmitRequest(
        tenant_id=tenant_id,
        platform=platform,
        external_user_id=external_user_id,
        channel_id=channel_id,
        thread_id=thread_id,
        role=role,
        role_ids=role_ids,
        verified_membership=verified_membership,
        is_dm=is_dm,
        dm_source_channel_id=dm_source_channel_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
        requester_org=requester_org,
        requester_home_id=requester_home_id,
        channel_shared=channel_shared,
        origin=origin,
        attended=attended,
        spend_person=spend_person,
        surface=surface,
        principal_platform=principal_platform,
        public_thread=public_thread,
        relay_scoped=result.relay_scoped,
        data_reading=result.data_reading,
    )
    return replace(result, observation=observation, request=request)


async def admit_helper(
    deps: TurnDeps, request: AdmitRequest, helper: HelperCall, *, now: datetime
) -> Admission:
    """Admit a helper's turn for the person whose turn asked it, as if they asked it.

    Every gate runs again for the helper's agent: whether they may run it here,
    their balance and caps, the channel's budget and its provider rules. It carries
    the asking turn's origin, so it is a person's invocation exactly when that is.
    """
    return await admit(
        deps,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        channel_id=request.channel_id,
        now=now,
        thread_id=request.thread_id,
        role=request.role,
        role_ids=request.role_ids,
        verified_membership=request.verified_membership,
        is_dm=request.is_dm,
        dm_source_channel_id=request.dm_source_channel_id,
        category_id=request.category_id,
        category_unresolved=request.category_unresolved,
        requester_org=request.requester_org,
        requester_home_id=request.requester_home_id,
        channel_shared=request.channel_shared,
        origin=request.origin,
        attended=request.attended,
        spend_person=request.spend_person,
        surface=request.surface,
        principal_platform=request.principal_platform,
        public_thread=request.public_thread,
        helper=replace(
            helper, relay_scoped=request.relay_scoped, data_reading=request.data_reading
        ),
    )


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
    role_ids: tuple[str, ...] = (),
    verified_membership: bool | None = None,
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    dm_agent_name: str | None = None,
    routine_agent_name: str | None = None,
    routine_thread_id: str | None = None,
    dm_no_source: bool = False,
    cold_dm: bool = False,
    category_id: str | None = None,
    category_unresolved: bool = False,
    requester_org: RequesterOrg,
    standing: StandingRequest | None = None,
    requester_home_id: str | None = None,
    channel_shared: bool | None = None,
    origin: str,
    attended: bool | None = None,
    spend_person: SpendPerson | None = None,
    surface: Surface | None = None,
    principal_platform: str | None = None,
    helper: HelperCall | None = None,
    public_thread: bool = False,
) -> Admission:
    """Run the full pre-turn gate sequence; raise instead of returning bool.

    M03: a message may move its conversation to the agent's switched provider
    (`_thread_follow`). When that move is refused there (e.g. the provider can't
    serve this agent yet), the message is admitted where the conversation already
    works instead: the move is written only after a successful admission, so there
    is nothing to withdraw and an unavailable preference never strands it.
    """
    gates = partial(
        _admit_gates,
        deps,
        tenant_id=tenant_id,
        platform=platform,
        external_user_id=external_user_id,
        channel_id=channel_id,
        now=now,
        thread_id=thread_id,
        role=role,
        role_ids=role_ids,
        verified_membership=verified_membership,
        is_dm=is_dm,
        dm_source_channel_id=dm_source_channel_id,
        dm_agent_name=dm_agent_name,
        routine_agent_name=routine_agent_name,
        routine_thread_id=routine_thread_id,
        dm_no_source=dm_no_source,
        cold_dm=cold_dm,
        category_id=category_id,
        category_unresolved=category_unresolved,
        requester_org=requester_org,
        standing=standing,
        requester_home_id=requester_home_id,
        channel_shared=channel_shared,
        origin=origin,
        attended=attended,
        spend_person=spend_person,
        surface=surface,
        principal_platform=principal_platform,
        helper=helper,
        public_thread=public_thread,
    )
    followed: list[Slot] = []
    try:
        return await gates(followed=followed)
    except AdmissionDenied as denied:
        if denied.reason != "backend_unsupported" or not followed:
            raise
    # Nothing was written for the move: admit where the conversation already works.
    return await gates(followed=[], follow_switch=False)


async def _admit_gates(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    channel_id: str,
    now: datetime,
    thread_id: str | None = None,
    role: Role | None = None,
    role_ids: tuple[str, ...] = (),
    verified_membership: bool | None = None,
    is_dm: bool = False,
    dm_source_channel_id: str | None = None,
    dm_agent_name: str | None = None,
    routine_agent_name: str | None = None,
    routine_thread_id: str | None = None,
    dm_no_source: bool = False,
    cold_dm: bool = False,
    category_id: str | None = None,
    category_unresolved: bool = False,
    requester_org: RequesterOrg,
    standing: StandingRequest | None = None,
    requester_home_id: str | None = None,
    channel_shared: bool | None = None,
    origin: str,
    attended: bool | None = None,
    spend_person: SpendPerson | None = None,
    surface: Surface | None = None,
    principal_platform: str | None = None,
    helper: HelperCall | None = None,
    public_thread: bool = False,
    followed: list[Slot] | None = None,
    follow_switch: bool = True,
) -> Admission:
    """The pre-turn gates (`admit_impl`).

    `follow_switch` lets this message move its conversation to the agent's switched
    provider (M03); each slot moved is added to `followed`, so a refused move can be
    withdrawn.
    """
    started = perf_counter()
    stage_ms: dict[str, float] = {}
    mark = stage_timer(stage_ms)
    # A helper's own conversation keys its session and provider slot; the place it
    # runs in (rules, seals, holds) stays the asking turn's thread.
    slot_thread_id = helper.thread_key if helper is not None else thread_id

    # --- Identity resolution ---
    async with deps.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session,
            tenant_id=tenant_id,
            platform=principal_platform or platform,
            external_id=external_user_id,
        )
        # Only a Discord server has a mode; every other platform is `off`. One read
        # serves this admission's standing, hold and facts.
        mode: AudienceMode = (
            await audience_mode(session, FeaturePlace(tenant_id, platform, channel_id))
            if platform == "discord"
            else "off"
        )
        if standing is not None and platform == "discord":
            requester_org = await requester_standing(
                session,
                tenant_id=tenant_id,
                user_id=external_user_id,
                channel_id=channel_id,
                is_dm=is_dm,
                event_role_ids=standing.event_role_ids,
                resolver=deps.membership_resolver,
                mode=mode,
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
        audience: ChannelAudience | None = None
        # A DM has no channel of its own to share; /dm-me asks about the channel
        # it copies history from.
        shared_place = dm_source_channel_id if is_dm else channel_id
        if shared_place is not None:
            audience = await _record_shared_channel(
                session,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=shared_place,
                thread_id=None if is_dm else thread_id,
                requester_org=requester_org,
                channel_shared=channel_shared,
                mode=mode,
                public_thread=public_thread,
            )
            shared = audience.contained
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
                channel=channel_ref(tenant_id, platform, channel_id),
                thread_id=slot_thread_id or "",
            ),
            account_id=str(principal.account_id),
        )
        if deps.backend_selection_enabled and slot_thread_id is not None and not is_dm:
            released = await release_due(deps.sessionmaker, state_store, own_slot)
        if is_dm and client:
            # A client has no private conversation with Daimon.
            raise AdmissionDenied(reason="client_requester")
        hold = TurnHold(platform, channel_id, client) if (client or shared) and not is_dm else None
        # Read after the role commit, so a refused turn still records the role.
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if public_thread:
            policy = with_public_thread_room(policy, channel_id=channel_id, thread_id=thread_id)
        mark("policy_and_admins")
        from daimon.core.native_admins import is_daimon_admin

        platform_admin = role is Role.ADMIN and not client
        admin = await is_daimon_admin(
            session,
            tenant_id=tenant_id,
            platform=platform,
            platform_user_id=external_user_id,
            is_platform_admin=platform_admin,
            role_ids=role_ids,
        )
        subject = build_subject(
            is_admin=admin and hold is None,
            client=client,
            platform_user_id=external_user_id,
            role_ids=role_ids,
            verified_membership=verified_membership,
        )
        if (
            platform == "discord"
            and (is_dm or verified_membership is None)
            and (is_dm or policy.invoker_role_ids or deps.membership_resolver is not None)
        ):
            subject, platform_admin = await resolve_subject_authority(
                deps.sessionmaker,
                deps.membership_resolver,
                tenant_id=tenant_id,
                user_id=external_user_id,
                require_fresh=is_dm,
                session=session,
            )
        platform_admin = platform_admin and not client
        effective_admin = subject.is_admin and hold is None and not client
        # Turn admission is not a delegated configuration action. Preserve the
        # effective verdict for the UI without granting runtime exemptions.
        subject = replace(subject, is_admin=platform_admin and hold is None, client=client)
        # Stored authority belongs only to live platform admins. A grant affects
        # this subject, never the hub's account/transcript admin exception.
        if role is not None and requester_org is RequesterOrg.HOST:
            await set_role(
                session, principal.account_id, Role.ADMIN if platform_admin else Role.USER
            )
        await session.commit()
        if (
            platform == "discord"
            and (
                (
                    verified_membership is None
                    and (policy.invoker_role_ids or deps.membership_resolver is not None)
                )
                or is_dm
            )
            and subject.verified_membership is not True
        ):
            raise AdmissionDenied(reason="invoker_not_allowed")
        if hold is not None:
            subject = replace(subject, is_admin=False, client=client)
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
            helper=helper.target if helper is not None else None,
        )
        config = await resolve_config(session, context=scope, default=deps.deployment_default)
        if dm_agent_name is not None:
            if not is_dm or thread_id is not None:
                raise ValueError("A cold DM agent override requires a fresh private scope")
            config = config.model_copy(
                update={"agent_name": dm_agent_name, "responder_ma_agent_id": None}
            )
        if routine_agent_name is not None:
            # A routine runs its own agent, not the channel's, in a fresh scope.
            if thread_id is not None:
                raise ValueError("A routine's agent override requires a fresh scope")
            config = config.model_copy(
                update={"agent_name": routine_agent_name, "responder_ma_agent_id": None}
            )
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
        # Name the backend as soon as it is known, so a failure while admitting still
        # says which provider it was on (the default Claude is named once preference is read).
        if backend_revision is not None:
            _name_backend(backend_revision)
        backend_footer_profile: str | None = None
        retained_revision: int | None = None
        backend_follow: ThreadFollow | None = None
        follow_eligible = (
            follow_switch
            and selection_enabled
            and channel is not None
            and slot_thread_id is not None
            and not is_dm
            and config.thread_binding_kind != "setup"
            and not configured_channel
            and not released
        )
        retained_binding: ProviderBinding | None = None
        retained_shared = False
        follow_generation: int | None = None

        async def read_retained(db: AsyncSession, *, shared_ok: bool = True) -> None:
            """Read the thread's retained binding: the shared slot first, then this caller's.

            Without `shared_ok` the shared slot is skipped (it was made for another agent).
            """
            nonlocal retained_revision, retained_binding, retained_shared
            nonlocal backend_revision, backend_revision_origin, backend_footer_profile
            assert channel is not None and provider_thread_id is not None
            thread = ThreadRef(channel=channel, thread_id=provider_thread_id)
            slots = [Slot(thread=thread, account_id=str(principal.account_id))]
            if backend_ever_shared:
                slots.insert(0, Slot(thread=thread))
            for slot in slots:
                if slot.account_id is None and not shared_ok:
                    continue
                if own_released and slot.account_id is not None:
                    continue  # A due release: this caller's slot selects afresh.
                binding = (
                    await deps.state_store.get_binding(slot)
                    if deps.state_store is not None
                    else await mux_state.get_binding(db, slot)
                )
                if binding is None:
                    continue
                if binding.thread != slot.thread or binding.legacy_account_id != slot.account_id:
                    raise ScopeViolation(
                        binding.id, "selected binding differs from authorized thread"
                    )
                retained_revision = binding.config_revision
                retained_binding, retained_shared = binding, slot.account_id is None
                try:
                    retained, _, source = await channel_backend.resolve_turn_backend(
                        db, channel, None, enabled=True, retained_revision=retained_revision
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
            if retained_revision is None and not own_released:
                # A caller's older session remains on its original default
                # backend even if neutral thread bindings did not yet exist.
                previous = await get_thread_session_at(
                    db,
                    tenant_id=tenant_id,
                    platform=platform,
                    thread_id=provider_thread_id,
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

        # A routine binds its own fresh scope but runs on the provider its thread kept,
        # read as `routine_provider` routes it (`effective_thread_backend`).
        provider_thread_id = slot_thread_id if routine_thread_id is None else routine_thread_id
        own_released = released
        if routine_thread_id is not None and channel is not None:
            own_released = await release_pending(
                session,
                Slot(
                    thread=ThreadRef(channel=channel, thread_id=routine_thread_id),
                    account_id=str(principal.account_id),
                ),
            )
        if (
            selection_enabled
            and channel is not None
            and provider_thread_id is not None
            and not is_dm
            and config.thread_binding_kind != "setup"
        ):
            await read_retained(session)

        if selection_enabled and backend_revision is not None:
            backend_footer_profile = backend_revision.profile
        # `read_retained` assigns it; pyright does not see a closure's write.
        if selection_enabled and channel is not None and cast(int | None, retained_revision) == 0:
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

    # A self-hosted agent preference without its operator grant is refused before
    # resolving its source through Anthropic. The name lookup can only deny; it never
    # selects a backend or grants authority to another resource with the same name.
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
                refused = (
                    preference is not None
                    and preference.backend == LOOP.provider
                    and not channel_backend.agent_grant_holds(tenant_id, preference)
                )
            else:
                from daimon.core.turn.runtimes import has_agent_grants

                refused = not has_agent_grants(
                    LOOP.profile_id, str(tenant_id)
                ) and await agent_backends.has_backend_preference_by_name(
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

    # A channel's self-hosted pin runs a harness record; an agent's own choice runs its source.
    selfhosted = (
        backend_revision is not None
        and backend_revision.backend == LOOP.provider
        and backend_revision_origin == "channel"
    )
    keyed = anthropic_configured(deps.anthropic)
    responder = config.responder_ma_agent_id
    # A Daimon record: self-hosted, a `dm_` responder, a name a record holds, or any
    # agent on a keyless install. Everything else is a Managed Agents agent, which needs the key.
    record_scope = Scope(
        tenant_id=str(tenant_id),
        account_id=str(principal.account_id),
        principal_id=external_user_id,
        authorization_id="turn-admission",
    )
    daimon_record = (
        selfhosted
        or (responder is not None and responder.startswith(DAIMON_ID_PREFIXES))
        or (not keyed and responder is None)
    )
    if not daimon_record and responder is None:
        from daimon.core.turn.selfhosted_records import names_a_record

        # A name a convert moved to a Daimon record means the record, not the archived copy.
        daimon_record = await names_a_record(deps.sessionmaker, record_scope, config.agent_name)
    if not daimon_record and not keyed:
        raise AdmissionDenied(
            reason="backend_unsupported",
            cause="anthropic_unconfigured",
            provider=MANAGED_AGENTS.provider,
        )
    if daimon_record:
        from daimon.core.turn.selfhosted_records import resolve_records

        resolve = partial(
            resolve_records,
            deps.sessionmaker,
            record_scope,
            agent_name=config.agent_name,
            environment_name=config.environment_name,
            responder_id=responder,
        )
        try:
            agent, environment = await resolve()
        except MAResolverMissError:
            # A keyless install seeds its defaults as Daimon records on first use.
            if selfhosted or keyed or dm_agent_name is not None:
                raise
            await reconcile_tenant_defaults(
                deps.anthropic,
                deps.sessionmaker,
                deps.defaults_root,
                tenant_id=tenant_id,
                public_url=deps.public_url,
            )
            try:
                agent, environment = await resolve()
            except MAResolverMissError as miss:
                if miss.kind != "agent":
                    raise
                # Not a Daimon record, so a Managed Agents one the key would reach.
                raise AdmissionDenied(
                    reason="backend_unsupported",
                    cause="anthropic_unconfigured",
                    provider=MANAGED_AGENTS.provider,
                ) from miss
        agent_id, env_id = agent.id, environment.id
        if (observation := current_outcome.get()) is not None:
            observation.agent_id = agent.id
    else:

        async def _apply() -> object:
            if dm_agent_name is not None:
                return None
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
        if config.thread_binding_kind in ("handoff", "owner", "web", "helper"):
            agent = await get_setup_agent(
                deps.anthropic, tenant_id=tenant_id, ma_agent_id=agent_id, records=deps.sessionmaker
            )
        elif config.thread_binding_id is not None:
            agent = await get_setup_responder(
                deps.anthropic, tenant_id=tenant_id, ma_agent_id=agent_id, records=deps.sessionmaker
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
                deps.anthropic, deps.resolver_cache, tenant_id, env_id, records=deps.sessionmaker
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
        if not daimon_record:
            from daimon.core.turn.selfhosted_records import names_a_record

            deps.resolver_cache.pop((tenant_id, "agent", config.agent_name), None)
            deps.resolver_cache.agents.pop((tenant_id, agent_id), None)
            # A convert archives the Managed Agents copy: the name's references are the record's.
            if not await names_a_record(deps.sessionmaker, record_scope, config.agent_name):
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
    from daimon.core.turn.invocation import human_invocation

    # An agent with a signed-identity MCP server answers with what its channel's
    # readers can read, so its turn stays in that channel (`daimon.core.relay_scope`).
    # A room whose stored facts say outsiders can read it (mode `on`) answers with no
    # signed-identity server at all, so such an agent is no relay agent there: its turn is
    # a plain hold with the room's own memory (`daimon.core.relay_scope`).
    if not is_dm:
        agent = _without_identity_servers(
            deps,
            agent,
            mode=mode,
            audience=audience,
            tenant_id=tenant_id,
            channel_id=channel_id,
            agent_name=config.agent_name,
        )
    # An agent that reads data sources is offered no data tool where outsiders may read (a
    # hold). A failed read is unknown: the turn runs with no data tool, and counts as
    # data-reading if the session read data or that cannot be told either.
    data_unknown = False
    try:
        data_reading = await reads_data(
            deps.sessionmaker,
            deps.settings,
            FeaturePlace.of(tenant_id, platform, channel_id, thread_id, agent_id=agent.id),
        )
    except Exception:
        _log.warning("turn.data_reads_lookup_failed", tenant_id=str(tenant_id), exc_info=True)
        data_reading, data_unknown = False, True
    data_on = data_reading
    if not data_reading:
        try:
            data_reading = await _session_read_data(
                deps, tenant_id, platform, slot_thread_id or channel_id, principal.account_id
            )
        except Exception:
            if not data_unknown:
                raise
            _log.warning("turn.data_session_lookup_failed", tenant_id=str(tenant_id))
            data_reading = True
    # Only data-reading agents ask each other: the helper's reads land on the asker's session
    # too (`daimon.adapters.mcp.data_access.ledgers`); either way round otherwise, data would
    # reach memory that does not record it.
    if helper is not None and data_reading != helper.data_reading:
        raise UserFacingError(AGENT_REQUEST_REFUSAL if data_reading else AFTER_DATA_REFUSAL)
    data_blocked = data_unknown or hold is not None
    relay_agent = (helper is not None and helper.relay_scoped) or agent_has_identity_relay(
        (server.url for server in agent.mcp_servers),
        public_url=deps.public_url,
        unscoped=deps.relay_unscoped,
    )
    if relay_agent and not is_dm:
        hold = (
            replace(hold, relay=True)
            if hold is not None
            else TurnHold(platform, channel_id, client=False, relay=True)
        )
        subject = replace(subject, is_admin=False)
        effective_admin = False
    if mode == "shadow" and not is_dm and audience is not None:
        _log_shadow_hold(
            audience.shadow,
            hold=hold,
            tenant_id=tenant_id,
            channel_id=channel_id,
            agent_name=config.agent_name,
        )
    grant = AdmissionGrant(
        tenant_id=tenant_id,
        subject=subject,
        human_invocation=human_invocation(origin, attended),
        surface=surface or (Surface.DM if is_dm else Surface.CHANNEL),
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
        platform=platform,
        cold_dm=cold_dm or dm_agent_name is not None,
        hold=hold,
        dm_no_source=dm_no_source,
        narrow_room=_narrow_by_facts(audience, mode) and not is_dm,
        public_thread=public_thread and not is_dm,
    )
    if agent_invoker_roles(policy, grant.agent.names):
        # Role gates always use fresh registered Discord membership, including
        # bound threads and routing/default changes discovered after selection.
        subject = await resolve_subject(
            deps.sessionmaker,
            deps.membership_resolver,
            tenant_id=tenant_id,
            user_id=external_user_id,
            require_fresh=True,
        )
        if hold is not None:
            subject = replace(subject, is_admin=False, client=hold.client)
        grant = replace(grant, subject=subject)
    _require_run_agent(policy, grant)
    mark("agent_policy")

    # Every id that seals the turn: its channel, and the thread sealed on its
    # own (a Discord thread by id, a Slack one as channel_id:thread_ts). All of
    # them are recorded, so unsealing one later leaves the others holding.
    seal_ids = limiting_ids_at(policy, channel_id=channel_id, thread_id=thread_id)
    if relay_agent and not is_dm:
        # What the relay answers is for this channel's readers: the session stays
        # sealed to the channel (and thread) it ran in, as for any limited channel.
        seal_ids |= relay_seal_ids(channel_id=channel_id, thread_id=thread_id)
    source_sealed = bool(seal_ids)
    memory_read_only = _memory_read_only(
        policy, grant, source_sealed, await _dm_memory_read_only(deps, grant)
    )

    # The agent's rules on daimon's own tools, read once per turn (no query
    # unless tool safety and agent rules are both on).
    daimon_rule_tools = await daimon_rule_tools_for(
        deps.sessionmaker, deps.tool_safety, tenant_id=tenant_id, agent_id=agent.id
    )
    mark("daimon_rule_tools")

    backend_counterpart: AgentRevision | None = None
    backend_agent_revision: AgentBackendRevision | None = None
    source_agent: BackendAgent | None = None

    # This conversation's own model choice, as the thread tier (`resolve_preference`).
    thread_choice: channel_backend.ThreadBackend | None = None

    def _thread_of(agent_revision: AgentBackendRevision | None) -> str | None:
        """The conversation whose revision `agent_revision` is, if the thread tier's."""
        chosen = thread_choice
        if chosen is None or chosen.consent is None or agent_revision is not chosen.consent:
            return None
        return chosen.thread_id

    resolved: dict[tuple[str, str], FeatureSnapshot] = {}

    async def features_at(admission: Admission) -> FeatureSnapshot:
        """The features this admission's turn runs with, read once: the counterpart check
        and the turn hide the same tools, or their copies differ."""
        key = (admission.agent.id, running_on(admission)["provider"])
        if key in resolved:
            return resolved[key]
        place = FeaturePlace.of(
            tenant_id, platform, channel_id, thread_id, agent_id=key[0], backend=key[1]
        )
        features = await place_features(deps, place, admission.agent)
        features = without_data_reads(features) if data_blocked else features
        resolved[key] = without_long_term_memory(features) if relay_agent else features
        return resolved[key]

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
        from daimon.core.turn.prepare import ProviderPreparationRequest, admitted_session_scope
        from daimon.core.turn.runtime_manifest import read_preparation_counterpart
        from daimon.core.turn.runtimes import scope_channel_runtimes

        provisional = Admission(
            is_platform_admin=platform_admin,
            effective_is_admin=effective_admin,
            account_id=principal.account_id,
            agent=agent,
            environment=environment,
            config=config.model_copy(update={"responder_ma_agent_id": agent.id}),
            grant=grant,
            memory_read_only=memory_read_only,
            asks_before_publishing=_asks_before_publishing(policy, grant),
            approval_gate=_approval_gate_for(deps, policy, grant, agent.id),
            daimon_rule_tools=daimon_rule_tools,
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
            backend_thread_id=_thread_of(agent_revision),
        )
        provisional = replace(provisional, features=await features_at(provisional))
        request = ProviderPreparationRequest(
            deps=deps,
            admission=provisional,
            scope=admitted_session_scope(
                provisional, tenant_id=tenant_id, session_id="preparation"
            ),
            tenant_id=tenant_id,
            platform=platform,
            external_user_id=external_user_id,
            thread_id=slot_thread_id or channel_id,
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
                if error.cause == "channel_row_missing":
                    raise error
                if (
                    error.cause in ("preparation_busy", "preparation_failed")
                    and error.provider is None
                ):
                    # The copy names the provider the switch went to (J6g); a busy
                    # or failed preparation is never "not set up".
                    raise AdmissionDenied(
                        reason="backend_unsupported",
                        cause=error.cause,
                        provider=revision.backend,
                    ) from error
            raise
        backend_counterpart = prepared.counterpart
        backend_agent_revision = agent_revision

    async def resolve_preference() -> None:
        nonlocal backend_revision, backend_ever_shared, backend, backend_revision_origin
        nonlocal backend_footer_profile, source_agent
        nonlocal retained_revision, backend_follow, follow_generation, thread_choice
        nonlocal retained_binding, retained_shared
        if selfhosted:
            return
        if selection_enabled and channel is not None and not configured_channel:
            async with deps.sessionmaker() as thread_session:
                thread_choice = await thread_backend(
                    thread_session,
                    tenant_id=tenant_id,
                    platform=platform,
                    conversation_id=conversation_key(
                        channel_id=channel_id, thread_id=thread_id, is_dm=is_dm
                    ),
                    agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id),
                    daimon_record=daimon_record,
                )
        if (
            selection_enabled
            and channel is not None
            and retained_revision
            and not configured_channel
        ):
            # A held revision runs only for the agent it was made for: after the
            # thread's responder changed, this slot selects afresh for the new one.
            turn_agent = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)
            async with deps.sessionmaker() as owner_session:
                owner = await mux_state.config_agent_id(owner_session, channel, retained_revision)
                if retained_shared and owner is not None and owner != turn_agent:
                    # The shared slot keeps the first agent's revision and cannot be
                    # released for one caller: skip it and read this caller's own slot.
                    retained_revision = None
                    retained_binding = None
                    retained_shared = False
                    backend_revision = None
                    backend_revision_origin = None
                    backend_footer_profile = None
                    await read_retained(owner_session, shared_ok=False)
                    owner = (
                        await mux_state.config_agent_id(owner_session, channel, retained_revision)
                        if retained_revision
                        else None
                    )
            if owner is not None and owner != turn_agent:
                follow_generation = retained_binding.generation if retained_binding else 0
                retained_revision = None
                backend_revision = None
                backend_revision_origin = None
                backend_footer_profile = None
        if follow_eligible and channel is not None and retained_revision is not None:
            # M03: the conversation follows its (Anthropic source) agent's switch.
            async with deps.sessionmaker() as follow_session:
                backend_follow = await _thread_follow(
                    follow_session,
                    tenant_id=tenant_id,
                    channel=channel,
                    agent_ma_id=agent.id,
                    bound_provider=(
                        backend_revision.backend
                        if backend_revision is not None
                        else MANAGED_AGENTS.provider
                    ),
                    bound_model=backend_revision.model if backend_revision is not None else None,
                    shared=retained_shared,
                    enabled_backends=tuple(policy.enabled_backends),
                    thread=thread_choice,
                    retained_local=retained_revision,
                )
            if backend_follow is not None and backend_follow.kind == "moved":
                # Select as released now; the release is written only once this
                # message is admitted (below), so a refusal leaves nothing behind.
                follow_generation = retained_binding.generation if retained_binding else 0
                retained_revision = None
                backend_revision = None
                backend_revision_origin = None
                backend_footer_profile = None
                if followed is not None:
                    followed.append(own_slot)
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

                async def select(
                    channel: ChannelRef = channel,
                ) -> tuple[ConfigRevision | None, bool, Literal["channel", "agent", "default"]]:
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
                            thread=thread_choice,
                        )
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
                        check_backend(backend_revision, enabled_backends=policy.enabled_backends)
                        if backend_revision is not None
                        else None
                    )
                except (channel_backend.AgentBackendRefused, BackendUnsupported) as error:
                    _log_backend_unsupported("agent_preference", error)
                    raise _backend_refusal(
                        backend_revision, source, policy.enabled_backends, error
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
        _name_backend(backend_revision)

    budget_channel_id = agent_permissions(policy, grant.agent.names).budget_channel or (
        dm_source_channel_id if is_dm else channel_id
    )
    spend_request = SpendRequest(
        tenant_id=tenant_id,
        platform_user_id=external_user_id,
        platform=platform,
        channel_id=budget_channel_id,
        agent_id=agent.id,
        person=spend_person,
    )
    # One gate decides balance, channel budget and allowance, in that order, on
    # one checked-out connection under READ COMMITTED.
    async with deps.sessionmaker() as billing_session:
        block = await spend_gate(
            billing_session,
            request=spend_request,
            markup=deps.markup,
            now=now,
        )
        mark("spend_gate")
        if block == "balance_depleted":
            from daimon.core.stores import tenant_ledger

            credited = await tenant_ledger.ever_credited(billing_session, tenant_id=tenant_id)
            raise AdmissionDenied(
                reason="balance_depleted", cause=None if credited else "no_credit_yet"
            )
        if block == "cap_exceeded":
            raise AdmissionDenied(reason="cap_exceeded")
        if block == "allowance_exhausted":
            raise AdmissionDenied(reason="allowance_exhausted")
        budget_exceeded = block == "budget_exceeded"
        channel_skill_rows = (
            []
            if is_dm or budget_exceeded
            else await list_channel_skills(
                billing_session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
            )
        )
    if budget_exceeded:
        notices = await place_feature(
            deps.sessionmaker,
            deps.settings,
            FeaturePlace.of(tenant_id, platform, budget_channel_id),
            "budget_notices",
        )
        if notices.on:
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
    register_spend_recheck(
        partial(
            spend_gate_in_new_session,
            deps.sessionmaker,
            request=spend_request,
            markup=deps.markup,
            checks=AFTER_QUEUE_CHECKS,
        )
    )

    # A scoped library skill the agent or channel carries mounts only where everyone the
    # turn reaches holds its scope; a change in who is here replaces the session (`skills`).
    scopes, held_scopes = await scoped_skill_audience(
        deps.sessionmaker,
        deps.settings,
        TurnPlace(
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=thread_id,
            asker_user_id=grant.subject.platform_user_id,
            asker_account_id=principal.account_id,
            ma_agent_id=agent.id,
            private=is_dm or grant.surface is Surface.WEB,
            closed=not data_on
            or data_blocked
            or shared
            or grant.hold is not None
            or grant.subject.client
            or helper is not None,
        ),
        [*(s.skill_id for s in agent.skills), *(r.skill_id for r in channel_skill_rows)],
        now=now,
    )
    withheld = frozenset(sid for sid, scope in scopes.items() if scope not in held_scopes)
    if daimon_record:
        # Daimon record skills are record references, resolved by the harness's
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
                held_scopes=held_scopes,
            )
        )

    mark("channel_skills")

    if deps.agent_backends:
        await resolve_preference()
        _name_backend(backend_revision)
    _refuse_managed_fallback(daimon_record=daimon_record, revision=backend_revision)
    platform_skills = backend_platform_skills(
        platform_skills, None if backend_revision is None else backend_revision.profile
    )
    channel_skills = (*platform_skills, *channel_skills)

    # The model this conversation's people chose, read fresh for every turn.
    backend_thread_id = _thread_of(backend_agent_revision) if backend_revision else None
    chosen = thread_choice.consent if thread_choice is not None else None
    thread_applied = (
        chosen is not None
        and backend_revision is not None
        and backend_revision.backend == chosen.backend
        and _effective_model(backend_revision.backend, backend_revision.model)
        == _effective_model(backend_revision.backend, chosen.model)
    )
    async with deps.sessionmaker() as session:
        thread_model = await apply_thread_model(
            session,
            agent=agent,
            tenant_id=tenant_id,
            platform=platform,
            conversation_id=conversation_key(
                channel_id=channel_id, thread_id=thread_id, is_dm=is_dm
            ),
            anthropic_backend=backend_revision is None or backend_revision.model is None,
            thread_applied=thread_applied,
        )
    agent = thread_model.agent
    if withheld:
        agent = agent.model_copy(
            update={"skills": [s for s in agent.skills if s.skill_id not in withheld]}
        )
    mark("thread_model")

    result = Admission(
        is_platform_admin=platform_admin,
        effective_is_admin=effective_admin,
        backend_revision=backend_revision,
        backend_revision_origin=backend_revision_origin,
        backend_counterpart=backend_counterpart,
        backend_agent_revision=backend_agent_revision,
        backend_thread_id=backend_thread_id,
        backend_footer_profile=backend_footer_profile,
        backend_follow=backend_follow,
        backend_ever_shared=backend_ever_shared,
        backend=backend,
        memory_read_only=memory_read_only,
        asks_before_publishing=_asks_before_publishing(policy, grant),
        approval_gate=_approval_gate_for(deps, policy, grant, agent.id),
        daimon_rule_tools=daimon_rule_tools,
        source_sealed=source_sealed,
        source_shared=source_shared,
        hold=hold,
        shared_channel=shared,
        relay_scoped=relay_agent,
        data_reading=data_reading,
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
        withheld_skills=withheld,
        grant=grant,
        model_override=thread_model.model_override,
        model_effort=thread_model.model_effort,
        model_fell_back=thread_model.fell_back,
        web=await _web_links(deps, tenant_id, principal.account_id, chat_admin=effective_admin),
    )
    result = replace(result, features=await features_at(result))
    await require_tool_limits_hold(deps, result)
    if follow_generation is not None:
        # Admitted: this conversation follows its agent (M03), only if its slot is
        # still the binding this message read (a concurrent move or card wins).
        await release_now(
            deps.sessionmaker,
            state_store,
            own_slot,
            generation=follow_generation,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            now=now,
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
        total_ms=elapsed_ms(started),
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
    all, so a lookup that failed once never demotes or promotes a stored role. OUTSIDER:
    nothing, as for UNKNOWN. A principal already marked foreign is written USER on any
    verdict but UNKNOWN and OUTSIDER,
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
    thread_id: str | None = None,
    requester_org: RequesterOrg,
    channel_shared: bool | None,
    mode: AudienceMode | None = None,
    public_thread: bool = False,
) -> ChannelAudience:
    """The channel's audience, recording it as shared when learned.

    The platform's live signal wins as the recorded source; a verified `OTHER`
    requester also reveals it. An `UNKNOWN` requester never marks a channel. A Discord
    turn never writes the permanent mark: its audience is live, so a room opens again
    when its outsider leaves.
    """
    source: Literal["platform", "client"] | None = None
    if platform == "discord":
        pass
    elif channel_shared is True:
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
        return ChannelAudience(contained=True, known=True, narrow=False, source="registry")
    return await channel_has_outsiders(
        session,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        mode=mode,
        public_thread=public_thread,
    )


def _narrow_by_facts(audience: ChannelAudience | None, mode: AudienceMode) -> bool:
    """Whether the stored facts call the room narrow, in mode `on`."""
    return mode == "on" and audience is not None and audience.source == "facts" and audience.narrow


def _without_identity_servers(
    deps: TurnDeps,
    agent: BetaManagedAgentsAgent,
    *,
    mode: AudienceMode,
    audience: ChannelAudience | None,
    tenant_id: uuid.UUID,
    channel_id: str,
    agent_name: str | None,
) -> BetaManagedAgentsAgent:
    """The agent as its room runs it: with no signed-identity server in a contained room.

    Only the stored facts of a Discord room in mode `on` take the servers away. Mode
    `shadow` leaves them and logs the agents that would lose them.
    """
    if audience is None or not identity_relay_server_names(agent, public_url=deps.public_url):
        return agent
    if mode == "on" and audience.source == "facts" and audience.contained:
        return without_identity_relays(agent, public_url=deps.public_url)[0]
    if mode == "shadow" and audience.shadow is not None and audience.shadow.contained:
        log_shadow(
            "tool_removed",
            tenant_id=tenant_id,
            channel_id=channel_id,
            agent=agent_name,
            reason="identity_server",
        )
    return agent


def _log_shadow_hold(
    shadow: ChannelAudience | None,
    *,
    hold: TurnHold | None,
    tenant_id: uuid.UUID,
    channel_id: str,
    agent_name: str | None,
) -> None:
    """Log a turn that mode `on` would hold and that nothing holds today."""
    if shadow is None or not shadow.contained or hold is not None:
        return
    log_shadow(
        "turn_held",
        tenant_id=tenant_id,
        channel_id=channel_id,
        agent=agent_name,
        reason="room_contained" if shadow.known else "room_unknown",
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
    if (
        (grant.act_for_creator or not grant.human_invocation)
        and agent_invoker_roles(policy, grant.agent.names)
        and not holds_agent_roles(policy, grant.agent.names, grant.subject)
    ):
        # Unattended: only a freshly verified owner holding every required role.
        raise AdmissionDenied(reason="invoker_not_allowed")
    subject = replace(grant.subject, is_admin=False) if grant.cold_dm else grant.subject
    if grant.cold_dm and not authorize(
        policy,
        subject=subject,
        action=Action.DIRECT_MESSAGE,
        agent=grant.agent,
        recipient_id=subject.platform_user_id,
    ):
        raise AdmissionDenied(reason="own_agents_only")
    decision = authorize(
        policy,
        subject=subject,
        action=Action.RUN_AGENT,
        surface=grant.surface,
        agent=grant.agent,
        place=grant.run_place,
        hold=grant.hold,
    )
    if not decision:
        raise AdmissionDenied(
            reason=decision.reason
            if decision.reason in ("own_agents_only", "invoker_not_allowed")
            else "runs_elsewhere"
        )


def _approval_gate_for(
    deps: TurnDeps,
    policy: TenantAccessPolicy,
    grant: AdmissionGrant,
    agent_id: str,
    *,
    dm_follows: bool = False,
) -> ApprovalGate | None:
    """None for a DM unless `dm_approvals` is on; a DM then reads the agent's or the
    workspace value, as it has no channel or thread rule of its own."""
    if (grant.is_dm and not dm_follows) or grant.platform is None or not deps.tool_safety.enabled:
        return None
    gate = resolve_approval_gate(
        policy,
        platform=grant.platform,
        channel_id=None if grant.is_dm else grant.channel_id,
        thread_id=None if grant.is_dm else grant.thread_id,
        agent_id=agent_id,
    )
    return replace(gate, tenant_id=grant.tenant_id)


async def refresh_approval_gate(deps: TurnDeps, admission: Admission) -> ApprovalGate | None:
    """Read the effective gate for this admitted tenant and actual origin, uncached."""
    grant = admission.grant
    dm_follows = admission.shown("dm_approvals")
    if (
        grant is None
        or grant.platform is None
        or (grant.is_dm and not dm_follows)
        or not deps.tool_safety.enabled
    ):
        return None
    async with deps.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=grant.tenant_id)
    return _approval_gate_for(deps, policy, grant, admission.agent.id, dm_follows=dm_follows)


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
) -> tuple[AdmissionGrant, bool, ChannelAudience | None]:
    """The grant, shared flag and the room's audience, with what was learned since admission;
    never relaxed.

    A requester marked foreign, or a channel recorded as shared, since the turn
    was admitted holds it to its channel now, drops its admin standing and makes
    the subject a client. Whatever the admission already held stays held. The audience
    is None for a DM and for a grant with no platform or requester.
    """
    shared = admission.shared_channel
    external_id = grant.subject.platform_user_id
    if grant.platform is None or external_id is None:
        return grant, shared, None
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
        # A DM that has no source by design (a private answer) has none: the budget
        # channel stands in for a source only when none was recorded.
        source = (
            grant.dm_source.channel_id
            if grant.dm_source is not None
            else None
            if grant.dm_no_source
            else admission.budget_channel_id
        )
        if (
            source is not None
            and (
                await channel_has_outsiders(
                    session, tenant_id=grant.tenant_id, platform=grant.platform, channel_id=source
                )
            ).contained
        ):
            raise DmSourceSealedError("dm_source_shared")
        return grant, shared, None
    client = grant.subject.client or (
        principal is not None and principal.foreign_home_id is not None
    )
    audience = await channel_has_outsiders(
        session,
        tenant_id=grant.tenant_id,
        platform=grant.platform,
        channel_id=grant.channel_id,
        thread_id=grant.thread_id,
        public_thread=grant.public_thread,
    )
    shared = shared or audience.contained
    platform = grant.platform
    if _narrow_by_facts(audience, "on") and not grant.narrow_room:
        grant = replace(grant, narrow_room=True)
    if not (client or shared):
        return grant, shared, audience
    hold = TurnHold(platform, grant.channel_id, client)
    if grant.hold is not None:
        hold = TurnHold(
            hold.platform,
            grant.hold.channel_id,
            client or grant.hold.client,
            relay=grant.hold.relay,
        )
    subject = replace(grant.subject, is_admin=False, client=hold.client)
    return replace(grant, subject=subject, hold=hold), shared, audience


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
        if grant.public_thread:
            policy = with_public_thread_room(
                policy, channel_id=grant.channel_id, thread_id=grant.thread_id
            )
        # The opt-in conversation policy is separate from native personal-chat
        # surfaces (for example Teams), which also admit with is_dm=True.
        if grant.is_dm and (grant.cold_dm or grant.dm_source is not None):
            from daimon.core.stores.direct_messages import dm_enabled
            from daimon.core.stores.tenants import get_tenant

            tenant = await get_tenant(session, grant.tenant_id)
            if (
                tenant is None
                or tenant.archived_at is not None
                or tenant.provision_status != "ready"
                or not await dm_enabled(session, tenant_id=grant.tenant_id)
            ):
                raise AdmissionDenied(reason="invoker_not_allowed")
        grant, shared_channel, audience = await _tightened_hold(session, admission, grant)
    if grant.subject.platform_user_id is not None and (
        policy.invoker_role_ids
        or agent_invoker_roles(policy, grant.agent.names)
        # A web chat's membership is the member mirror's: only a role rule asks again.
        or (
            deps.membership_resolver is not None
            and grant.subject.verified_membership is not None
            and grant.surface is not Surface.WEB
        )
    ):
        subject = await resolve_subject(
            deps.sessionmaker,
            deps.membership_resolver,
            tenant_id=grant.tenant_id,
            user_id=grant.subject.platform_user_id,
            # The web app gets no gateway events to expire its cache: it always asks.
            require_fresh=grant.act_for_creator
            or grant.surface is Surface.WEB
            or bool(agent_invoker_roles(policy, grant.agent.names)),
        )
        if subject.verified_membership is not True:
            raise AdmissionDenied(reason="invoker_not_allowed")
        if grant.hold is not None:
            subject = replace(subject, is_admin=False, client=grant.hold.client)
        grant = replace(grant, subject=subject)
        admission = replace(admission, grant=grant)
    if grant.act_for_creator and (
        not authorize(
            policy, subject=grant.subject, action=Action.ACT_FOR_CREATOR, surface=Surface.ROUTINE
        )
        or not env_admits(
            deps.direct_message_policies, grant.tenant_id, grant.subject.platform_user_id or ""
        )
    ):
        raise AdmissionDenied(reason="invoker_not_allowed")
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
        policy, grant, bool(seal_ids), await _dm_memory_read_only(deps, grant)
    )
    asks_before_publishing = _asks_before_publishing(policy, grant)
    approval_gate = _approval_gate_for(deps, policy, grant, admission.agent.id)
    rule_tools = await daimon_rule_tools_for(
        deps.sessionmaker, deps.tool_safety, tenant_id=grant.tenant_id, agent_id=admission.agent.id
    )
    # A room that became contained since admission answers with no signed-identity
    # server: the session is built from this agent.
    agent = admission.agent
    if audience is not None and audience.source == "facts" and audience.contained:
        agent = without_identity_relays(agent, public_url=deps.public_url)[0]
    if (
        agent is admission.agent
        and grant == admission.grant
        and shared_channel == admission.shared_channel
        and seal_ids == admission.origin_seal_ids
        and memory_read_only == admission.memory_read_only
        and asks_before_publishing == admission.asks_before_publishing
        and approval_gate == admission.approval_gate
        and rule_tools == admission.daimon_rule_tools
    ):
        return admission
    return replace(
        admission,
        agent=agent,
        grant=grant,
        hold=grant.hold,
        shared_channel=shared_channel,
        origin_seal_ids=seal_ids,
        source_sealed=admission.source_sealed or bool(seal_ids),
        memory_read_only=memory_read_only,
        asks_before_publishing=asks_before_publishing,
        approval_gate=approval_gate,
        daimon_rule_tools=rule_tools,
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


async def require_current_spend(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    budget_channel_id: str,
    agent_id: str,
    now: datetime,
) -> None:
    """The fresh spend gate for a watch send or report."""
    async with deps.sessionmaker() as session:
        block = await spend_gate(
            session,
            request=SpendRequest(
                tenant_id=tenant_id,
                platform_user_id=external_user_id,
                platform=platform,
                channel_id=budget_channel_id,
                agent_id=agent_id,
            ),
            markup=deps.markup,
            now=now,
        )
    if block == "budget_exceeded":
        raise AdmissionDenied(reason="channel_budget_exceeded")
    if block is not None:
        raise AdmissionDenied(reason=block)


def decide_before_send(
    deps: TurnDeps, admission: Admission, *, attended: bool = True
) -> Callable[[], Awaitable[None]]:
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
            or (
                attended
                and admission.approval_gate is not None
                and admission.approval_gate.value == "off"
                and (current.approval_gate is None or current.approval_gate.value != "off")
            )
        ):
            raise SessionBusyError(
                pending_reasons=("seal",), retry_after=datetime.now(UTC) + timedelta(seconds=1)
            )

    return decide


async def _dm_memory_read_only(deps: TurnDeps, grant: AdmissionGrant) -> bool:
    """The `dm_memory_read_only` feature for a DM turn; not read for any other turn. One
    workspace-wide setting, so the platform only has to be one that offers it."""
    if not grant.is_dm:
        return False
    # Lazy: host_memory reaches the rule views, which import the turn package.
    from daimon.core.host_memory import audience_memory_read_only

    place = FeaturePlace(grant.tenant_id, "discord")
    async with deps.sessionmaker() as session:
        setting = (await feature_in(session, None, place, "dm_memory_read_only")).on
        # The audience mode makes a DM's shared memory read-only too: the one rule.
        return setting or await audience_memory_read_only(
            session,
            place,
            agent=next((name for name in grant.agent.names if name), None),
            already_read_only=setting,
        )


def _memory_read_only(
    policy: TenantAccessPolicy, grant: AdmissionGrant, sealed: bool, dm_read_only: bool
) -> bool:
    """A sealed room is read-only for every agent but its own; a narrow room (by its
    stored facts) is read-only for its own agent too."""
    return (sealed and not _is_own_agent(policy, grant)) or dm_read_only or grant.narrow_room


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
    Logs one `backend_source.capture_timing` line: its stages, and the outcome.
    """
    started, stage_ms = perf_counter(), dict[str, float]()
    outcome, detail = "failed", None
    try:
        captured = await _capture_backend_source(
            deps,
            source,
            context=context,
            subject=subject,
            place=place,
            is_dm=is_dm,
            slack_turn_context_id=slack_turn_context_id,
            private_dm_id=private_dm_id,
            external_user_id=external_user_id,
            explicit_target_name=explicit_target_name,
            responder_ma_agent_id=responder_ma_agent_id,
            stage_ms=stage_ms,
        )
        outcome = "captured"
        return captured
    except channel_backend.AgentBackendRefused as error:
        outcome = error.reason
        detail = error.detail if error.detail in channel_backend.REFUSAL_DETAILS else None
        raise
    finally:
        _log.info(
            "backend_source.capture_timing",
            tenant_id=str(context.tenant_id),
            agent_id=str(source.agent_id),
            platform=context.platform,
            channel_id=context.channel_id,
            thread_id=context.thread_id,
            outcome=outcome,
            detail=detail,
            total_ms=elapsed_ms(started),
            stage_ms=stage_ms,
        )


async def _capture_backend_source(
    deps: TurnDeps,
    source: BackendAgent,
    *,
    context: ScopeContext,
    subject: Subject,
    place: Place,
    is_dm: bool,
    slack_turn_context_id: uuid.UUID | None,
    private_dm_id: str | None,
    external_user_id: str | None,
    explicit_target_name: str | None,
    responder_ma_agent_id: str | None,
    stage_ms: dict[str, float],
) -> BackendAgent:
    from daimon.core.defaults.ma_index import find_environment_by_daimon_tag
    from daimon.core.mux_backend import resource_scope
    from daimon.core.mux_compat import retrieve_agent
    from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.stores.identity import list_tenant_chat_platforms
    from daimon.core.turn.prepare import ProviderPreparationRequest
    from daimon.core.turn.runtime_manifest import capture_preparation_source

    mark = stage_timer(stage_ms)
    try:
        async with asyncio.timeout(PREVIEW_CAPTURE_BUDGET_S):
            if context.tenant_id != source.tenant_id or context.account_id is None:
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="identity")
            if source.source is None:
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="source")
            async with deps.sessionmaker() as db:
                account = await get_account_with_tenant(db, account_id=context.account_id)
                policy = await load_access_policy(db, tenant_id=context.tenant_id)
                config = await resolve_config(db, context=context, default=deps.deployment_default)
                answers_on = (
                    await list_tenant_chat_platforms(db, tenant_id=context.tenant_id)
                    if explicit_target_name is not None
                    else ()
                )
            mark("config")
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
            # Independent reads: one round trip, not two.
            actual, environment = await asyncio.gather(
                retrieve_agent(
                    deps.anthropic, source.source.ref.id, scope=scope, records=deps.sessionmaker
                ),
                find_environment_by_daimon_tag(
                    deps.anthropic,
                    tenant_id=context.tenant_id,
                    name=config.environment_name,
                    records=deps.sessionmaker,
                ),
            )
            mark("agent_environment_reads")
            if explicit_target_name is not None:
                await channel_backend.load_backend_agent(
                    deps.anthropic,
                    deps.sessionmaker,
                    tenant_id=context.tenant_id,
                    agent_name=explicit_target_name,
                    actual_agent=actual,
                )
                mark("load_backend_agent")
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
                platform=context.platform or account.platform,
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
            mark("place")
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
            rule_tools = await daimon_rule_tools_for(
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
                memory_read_only=_memory_read_only(
                    policy, grant, bool(seals), await _dm_memory_read_only(deps, grant)
                ),
                asks_before_publishing=_asks_before_publishing(policy, grant),
                approval_gate=_approval_gate_for(deps, policy, grant, actual.id),
                daimon_rule_tools=rule_tools,
                source_sealed=bool(seals),
                origin_channel_id=context.channel_id,
                origin_thread_id=context.thread_id,
                origin_seal_ids=seals,
                channel_skills=skills,
                slack_turn_context_id=slack_turn_context_id,
                private_dm_id=(private_dm_id or context.thread_id or context.channel_id)
                if is_dm
                else private_dm_id,
                web=await _web_links(
                    deps, context.tenant_id, context.account_id, chat_admin=grant.subject.is_admin
                ),
            )
            # An agent's own read is of its chat turns: its features are the tenant's.
            read_on = (
                account.platform
                if explicit_target_name is not None
                else (context.platform or account.platform)
            )
            # The turn leaves out what is Off here, so the preview names none of it as lost.
            # Only those keys: the target, which narrows others by backend, is not known here.
            ids = (context.tenant_id, read_on, context.channel_id, context.thread_id)
            here = FeaturePlace.of(*ids, agent_id=actual.id)
            features = await place_features(deps, here, actual, HIDING_KEYS)
            data_reading = features.on(DATA_READS) or await _session_read_data(
                deps,
                context.tenant_id,
                read_on,
                context.thread_id or context.channel_id or "",
                context.account_id,
            )
            relay = agent_has_identity_relay(
                (server.url for server in actual.mcp_servers),
                public_url=deps.public_url,
                unscoped=deps.relay_unscoped,
            )
            features = without_long_term_memory(features) if relay else features
            admission = replace(admission, features=features, data_reading=data_reading)
            mark("admission")
            request = ProviderPreparationRequest(
                deps,
                admission,
                scope,
                context.tenant_id,
                read_on,
                label,
                context.thread_id or "",
                context.account_id,
                False,
                DEFAULT_MA_CAPABILITIES,
                None,
                datetime.now(UTC) + timedelta(seconds=PREVIEW_CAPTURE_BUDGET_S),
                lambda: datetime.now(UTC),
                answers_on=answers_on,
            )
            captured = await capture_preparation_source(
                request, fresh=True, capability_preview=True, stage_ms=stage_ms
            )
            if (captured.tenant_id, captured.agent_id) != (source.tenant_id, source.agent_id):
                raise channel_backend.AgentBackendRefused("source_unavailable", detail="source")
            return captured
    except (APIError, TimeoutError) as error:
        _log.warning(
            "backend_source.unavailable", causes=channel_backend.backend_refusal_causes(error)
        )
        raise channel_backend.AgentBackendRefused("source_unavailable") from error
    except TurnError as error:
        # A capture blip asks for a retry; it never confirms a loss.
        if channel_backend.transient_failure(error):
            _log.warning(
                "backend_source.unavailable", causes=channel_backend.backend_refusal_causes(error)
            )
            raise channel_backend.AgentBackendRefused("source_unavailable") from error
        raise
    except ScopeViolation as error:
        raise channel_backend.AgentBackendRefused("source_unavailable", detail="place") from error


async def capture_agent_source(
    deps: TurnDeps,
    source: BackendAgent,
    *,
    account_id: uuid.UUID,
    subject: Subject,
    external_user_id: str | None = None,
) -> BackendAgent:
    """Capture the named agent's own source: the requester's tenant and account, no channel.

    The agent-scoped form of `capture_backend_source`, for settings that belong to the
    agent wherever it answers (provider and model). `external_user_id` names a requester
    whose account holds no principal on the tenant's own platform (a web person).
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
        external_user_id=external_user_id,
        explicit_target_name=source.name,
    )


_TURN_FEATURE_KEYS: Final = tuple(
    dict.fromkeys(
        (
            *TURN_KEYS,
            *SESSION_KEYS,
            *PER_TURN_KEYS,
            *IDENTITY_KEYS,
            *HISTORY_KEYS,
            *LIMIT_KEYS,
            *CHAT_KEYS,
            # Read with the rest, so the output gate asks the database nothing more.
            "audience_hold",
        )
    )
)


async def _session_read_data(
    deps: TurnDeps, tenant_id: uuid.UUID, platform: str, thread_id: str, account_id: uuid.UUID
) -> bool:
    """Whether the live session here read data: it counts as data-reading until `/new`,
    whatever `data_reads` says now and with every source retired, as its context holds what
    it read. A workspace that never read data skips the lookup."""
    if not thread_id or (deps.settings is not None and not data_sources_on(deps.settings)):
        return False
    async with deps.sessionmaker() as session:
        if not await tenant_has_reads(session, tenant_id=tenant_id):
            return False
        live = await get_live_thread_session(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id,
            account_id=account_id,
        )  # fmt: skip
    return (
        live is not None
        and live.effective_config is not None
        and live.effective_config.data_read
        and live.fresh_start_requested_at is None
    )


async def place_features(
    deps: TurnDeps,
    place: FeaturePlace,
    agent: BetaManagedAgentsAgent,
    keys: tuple[str, ...] = _TURN_FEATURE_KEYS,
) -> FeatureSnapshot:
    """The features a turn of `agent` reads at `place`; its toolset hides tools by them. Data
    tools hide until the workspace has a source."""
    features = await resolve_features(
        deps.sessionmaker,
        deps.settings,
        place,
        keys=keys,
        agent_builtins=builtins_of(agent),
        server_setup=False,
    )
    data = features.features.get(DATA_SOURCES)
    if (
        data is None
        or not data.on
        or deps.settings is None
        or await has_sources(deps.sessionmaker, deps.settings, place.tenant_id)
    ):
        return features
    return without_data_sources(features)


async def require_tool_limits_hold(deps: TurnDeps, admission: Admission) -> None:
    """Refuse a channel's own Gemini or OpenAI deployment where a channel or thread
    switched a built-in tool off.

    An agent's own copy leaves such a tool out of the reply (`runtime_manifest`), as
    Claude does. A channel's deployment is the operator's fixed agent: running it anyway
    would quietly give the agent a tool the people there turned off.
    """
    native = admission.features
    if (
        native is None
        or native.place.backend not in (INLINE_REUSE.provider, PERSISTENT_WORKSPACE.provider)
        or admission.backend_revision_origin == "agent"
    ):
        return
    above = await place_features(
        deps,
        replace(native.place, channel_id=None, thread_id=None),
        admission.agent,
        keys=TOOL_KEYS,
    )
    if any(above.on(key) and not native.on(key) for key in TOOL_KEYS):
        raise AdmissionDenied(
            reason="backend_unsupported", cause="tool_limits", provider=native.place.backend
        )


def running_on(admission: Admission) -> dict[str, str]:
    """The model provider and model this message actually runs on: what the card's
    footer and the metering say, never the agent's settings (M03)."""
    from daimon.core.turn.shown_model import shown_model

    shown = shown_model(admission)
    return {"provider": shown.provider, "model": shown.id}


async def _web_links(
    deps: TurnDeps, tenant_id: uuid.UUID, account_id: uuid.UUID, *, chat_admin: bool
) -> TenantLinks | None:
    """The tenant's web links for the web role the account holds; None with no web app or
    for a suspended person."""
    if deps.web_links is None:
        return None
    async with deps.sessionmaker() as session:
        role = await role_of_account(
            session, tenant_id=tenant_id, account_id=account_id, chat_admin=chat_admin
        )
    return None if role is None else deps.web_links.for_tenant(tenant_id, admin=is_web_admin(role))


def backend_facts(admission: Admission) -> dict[str, Any]:
    """`render_turn_origin`'s backend facts for this admitted message (M03), and its web links."""
    follow = admission.backend_follow
    return {
        "web": admission.web,
        "features": admission.features,
        "running_on": running_on(admission),
        # A later hand-off summary (#1434) slots in here beside the move.
        "moved": None
        if follow is None
        else {"kind": follow.kind, "provider": follow.provider, "model": follow.model},
    }

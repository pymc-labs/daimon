"""Stage two of the two-stage turn chokepoint: `bind_session()`.

A single core call performs thread-session find-or-create, the full
`create_session` kwarg assembly, the `thread_sessions` mapping write, and
usage-recorder binding — returning a frozen `PreparedTurn` whose recorder is
a non-public field. Adapters never see or construct billing wiring.

`fernet=deps.fernet` is unconditional here: this is the fix for SPEC Req
7(a), the historical Slack gap where `create_session` was called without a
`fernet` argument.

The decision of whether the existing session may be reused, refreshed in
place or must be replaced lives in `daimon.core.session_preparation`; this
module keeps the two primitives that decision drives — creating a session and
binding its recorder — and hands them over as `SessionOps`. The dependency
runs one way, and `create_session` stays a name in THIS module, which is what
every adapter test patches.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, cast

import anthropic as anthropic_pkg
import structlog
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_system_content_block_param import (
    BetaManagedAgentsSystemContentBlockParam,
)
from anthropic.types.beta.session_create_params import Resource
from anthropic.types.beta.sessions.beta_managed_agents_github_repository_resource import (
    BetaManagedAgentsGitHubRepositoryResource,
)
from daimon.core.channel_backend import BackendUnsupported
from daimon.core.channel_skills import PLATFORM_SKILL_RETRY_AFTER
from daimon.core.config import get_repo_memory_settings, load_turn_settings
from daimon.core.confirmation import confirmed_tools_for_tenant
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.github_app_session import AppSessionAccess, token_scopes_for_tokens
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_hold import hold_key
from daimon.core.memory_resource import memory_scope
from daimon.core.mux_backend import TurnRuntime, managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import legacy_call
from daimon.core.native_footer_cost import NativeFooterCostContext
from daimon.core.permissions import any_readers_limited
from daimon.core.platform_system import system_for_platform
from daimon.core.pricing import MODEL_PRICING
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES, ChangeReason, MaCapabilities
from daimon.core.session_seal import inherited_seal_ids, origin_stamp, seal_ids
from daimon.core.session_snapshot import (
    MaSkill,
    SessionSnapshot,
    TokenScope,
    fingerprint_identity,
    fingerprint_mutable,
    hash_env_bytes,
    session_skills,
    snapshot_from_created_session,
)
from daimon.core.sessions import create_session
from daimon.core.shared_threads import (
    SharedThread,
    may_have_shared_binding,
    record_shared_binding,
    resolve_shared_thread,
)
from daimon.core.skills.unresolved import SkillUnavailable
from daimon.core.stores import mux_state
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.agent_memory_stores import get_personal_memory_store_id
from daimon.core.stores.domain import BackendAgent, TransferKind
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
    read_session_seals,
    record_session_seals,
)
from daimon.core.turn.admission import Admission, reauthorize, restrict_inherited_memory
from daimon.core.turn.ceiling import ceiling_error, remaining_s, turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError, SessionPreparationFailed
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.posture import UsageRecorder
from daimon.core.turn.runtimes import ensure_channel_runtime, scope_channel_runtimes
from daimon.core.turn_origin import SessionState
from daimon.core.usage_billing import ObservationRecorder
from daimon.core.usage_recording import record_turn_usage
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.resources import AgentSpec, SessionSpec
from mux.drivers.anthropic import SessionReads
from mux.errors import ScopeViolation
from mux.profiles import get_profile
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from daimon.core.session_preparation_stages import WorkspaceTransfer
    from daimon.core.stores.neutral_catalog import AgentRevision

log = structlog.get_logger(__name__)


def _delivered_app_snapshot_state(
    access: AppSessionAccess | None,
) -> tuple[bool, tuple[str, ...], tuple[TokenScope, ...], dict[int, int]]:
    """Use delivered token identity; later grant changes belong to compatibility."""
    if access is None or not access.tokens:
        return False, (), (), {}
    multi_installation = any(token.chunk is not None for token in access.tokens)
    if multi_installation != access.multi_installation:
        raise ValueError("delivered App token labels disagree with the vault mode")
    scopes = token_scopes_for_tokens(access.tokens, include=multi_installation)
    issued_at = {
        installation_id: min(
            token.issued_at
            for token in access.tokens
            if token.installation_id == installation_id and token.issued_at is not None
        )
        for installation_id in {scope[0] for scope in scopes}
    }
    return True, access.token_repo_urls, scopes, issued_at


@dataclass(frozen=True, slots=True)
class ContinuityOutcome:
    """What this bind did to the caller's session, for the adapter to say so.

    The default is the honest description of every turn before this existed and
    of every turn where nothing changed: the session continued.
    """

    state: Literal["continued", "updated", "replaced", "replaced_after_loss"] = "continued"
    applied: tuple[ChangeReason, ...] = ()
    pending: tuple[ChangeReason, ...] = ()
    transfer_kind: TransferKind | None = None
    user_prefix: str = ""
    system_blocks: tuple[BetaManagedAgentsSystemContentBlockParam, ...] = ()

    def session_state(self) -> SessionState:
        """This bind as `<turn_controls>` facts.

        `lost` follows the replacement's transfer degrade ladder, which already
        says what did not survive; that keeps the controls' "say what is
        missing" instruction accurate.
        """
        lost = _LOST_BY_TRANSFER.get(self.transfer_kind, ())
        return SessionState(state=self.state, applied=self.applied, lost=lost)

    def announces_replacement(self) -> bool:
        """Whether the person should be told this bind replaced their session.

        Only what a person would notice is announced: a different agent or
        model now answering (`NOTICEABLE_REPLACEMENT_REASONS`), or something
        that did not come across (anything short of a full transfer). Every
        other reason -- instructions, skills, tools, MCP servers, `.env`, repo
        token age, GitHub mode or repositories (including ones the person just
        connected), memory, vault, environment -- carried everything over and
        changes nothing they can see, so a notice would only puzzle them.
        """
        if self.state != "replaced" or self.transfer_kind is None:
            return False
        if self.transfer_kind != "full":
            return True
        return any(reason in NOTICEABLE_REPLACEMENT_REASONS for reason in self.applied)


#: The replacement reasons a person notices on their own: a different agent
#: or a different model answers. Only these, or a transfer that lost
#: something, are announced (`ContinuityOutcome.announces_replacement`).
NOTICEABLE_REPLACEMENT_REASONS: frozenset[ChangeReason] = frozenset({"agent_identity", "model"})

_LOST_BY_TRANSFER: dict[TransferKind | None, tuple[str, ...]] = {
    "transcript": ("working files",),
    "history": ("working files", "earlier conversation"),
}
CONTINUED = ContinuityOutcome()


@dataclass(frozen=True)
class PreparedTurn:
    """Everything `run_prepared_turn` needs to drive a turn.

    `_record` is intentionally underscore-prefixed and excluded from the
    public contract adapters consume — the recorder is reachable only
    through `run_prepared_turn`.
    """

    admission: Admission
    ma_session_id: str
    mapping_id: uuid.UUID | None
    watermark: str | None
    reused: bool
    session_account_id: uuid.UUID
    _record: UsageRecorder | ObservationRecorder
    continuity: ContinuityOutcome = CONTINUED
    # Capability decisions follow the session snapshot, including reused sessions.
    session_model_id: str | None = field(default=None, kw_only=True)
    _finalize: Callable[[], Awaitable[None]] | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    # Populated only by a provider-native preparation hook. Anthropic's
    # existing field values and preparation remain unchanged.
    backend: ManagedAgents | None = None
    session_ref: ResourceRef | None = None
    runtime: TurnRuntime | None = None
    native_footer_cost: NativeFooterCostContext | None = None
    native_chat_session_id: str | None = field(default=None, kw_only=True)
    _scope: Scope | None = field(default=None, kw_only=True, compare=False, repr=False)
    _native_spec: AgentSpec | None = field(default=None, kw_only=True, compare=False, repr=False)


@dataclass(frozen=True)
class ProviderPreparationRequest:
    deps: TurnDeps
    admission: Admission
    scope: Scope
    tenant_id: uuid.UUID
    platform: str
    external_user_id: str
    thread_id: str
    session_account_id: uuid.UUID
    reuse_existing: bool
    capabilities: MaCapabilities
    transfer: WorkspaceTransfer | None
    deadline: dt.datetime
    now: Callable[[], dt.datetime]
    counterpart: AgentRevision | None = field(default=None, kw_only=True)
    verified_counterpart: AgentRevision | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    source: BackendAgent | None = field(default=None, kw_only=True, compare=False, repr=False)
    target_spec: AgentSpec | None = field(default=None, kw_only=True, compare=False, repr=False)
    # The tenant's hosted OpenAI environment, created or reused for this turn by
    # `openai_environment.tenant_environment`; the code-default GPT row uses it.
    openai_environment_id: str | None = field(default=None, kw_only=True, compare=False, repr=False)


ProviderPreparation = Callable[[ProviderPreparationRequest], Awaitable[PreparedTurn]]
_PROVIDER_PREPARATIONS: dict[str, ProviderPreparation] = {}


def register_turn_preparation(
    profile: str, prepare: ProviderPreparation, *, fallback: bool = False
) -> None:
    """Register provider-native binding; missing hooks never use Anthropic IDs."""
    if profile == "anthropic.managed_agents":
        raise ValueError(f"turn preparation already registered: {profile}")
    if profile in _PROVIDER_PREPARATIONS:
        if fallback:
            return
        raise ValueError(f"turn preparation already registered: {profile}")
    _PROVIDER_PREPARATIONS[profile] = prepare


def _admitted_profile(admission: Admission) -> str:
    return (
        admission.backend_revision.profile
        if admission.backend_revision is not None
        else "anthropic.managed_agents"
    )


@dataclass(frozen=True)
class FreshSession:
    """A just-created MA session, its mapping row, and the config it froze.

    `snapshot` is what the session will run for the rest of its life: MA
    freezes the agent at creation time, so this — not `admission.agent` — is
    what a later turn must bill and compare against.
    """

    ma_session_id: str
    mapping_id: uuid.UUID
    snapshot: SessionSnapshot
    # The admission as decided again right before the session was created
    # (`reauthorize`), so the turn runs under what the session was built with.
    admission: Admission | None = None


__all__ = [
    "ContinuityOutcome",
    "FreshSession",
    "PreparedTurn",
    "bind_recorder",
    "bind_session",
    "CreatedSession",
    "create_fresh_session",
    "create_ma_session",
    "insert_mapping",
    "get_live_thread_session",
]


async def _env_bytes_sha256(
    deps: TurnDeps, *, tenant_id: uuid.UUID, agent_uuid: uuid.UUID
) -> str | None:
    """Hash of the `.env` bytes `create_session` is about to mount, or None.

    Mirrors `credential_env.upload_env_and_mount`: the same tenant-scoped rows
    through the same `assemble_env_bytes`, and None for an agent with no
    secrets (that agent gets no `.env` resource at all). Read BEFORE the
    session is created, deliberately — a key written while `create_session`
    runs then records as stale rather than as current, and a stale hash costs
    one redundant refresh, where a falsely-current one would leave the session
    silently running the old secrets.
    """
    async with deps.sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_uuid)
    rows = [row for row in rows if row.key not in ("GH_TOKEN", "GITHUB_TOKEN")]
    if not rows:
        return None
    return hash_env_bytes(assemble_env_bytes(rows))


@dataclass(frozen=True)
class CreatedSession:
    """An MA session that exists upstream but has no mapping row yet."""

    ma_session_id: str
    snapshot: SessionSnapshot
    # The admission as decided again right before `sessions.create`.
    admission: Admission | None = None
    seal_ids: frozenset[str] = frozenset()
    # The id its mapping row is written under, chosen before the session exists
    # so the session's signed-identity relay credential can name it.
    mapping_id: uuid.UUID = field(default_factory=uuid.uuid4)


async def create_ma_session(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    extra_resources: tuple[Resource, ...] = (),
    predecessor_session_id: str | None = None,
    platform: str,
) -> CreatedSession:
    """Create a brand-new MA session and snapshot the configuration it froze.

    The single shared `create_session` call site for a fresh session -- a
    divergent second call site is exactly the bug shape this phase exists to
    kill. The snapshot is taken from the object `sessions.create` returned,
    the authority on what the session will execute.

    A session that replaces `predecessor_session_id` -- by transcript,
    checkpoint, bundle, handoff or dead-session recovery -- carries its
    predecessor's work, so it is stamped with its predecessor's seal as well
    as this turn's (`daimon.core.session_seal.inherited_seal_ids`).

    Every session is created here -- a thread's first, a replacement after a
    checkpoint or workspace transfer, a dead-session successor -- so the
    admission is decided again here, on the policy as it is at this moment
    (`reauthorize`): a pin added during a transfer or lock wait refuses the
    turn before any session exists, and a seal added since is stamped on the
    session and mounts memory read-only.

    `platform` picks the system prompt the session runs (`system_for_platform`),
    derived here from the executing agent's own system.
    """
    if _admitted_profile(admission) != "anthropic.managed_agents":
        raise AdmissionDenied(reason="backend_unsupported")
    admission = await reauthorize(deps, admission)
    seal: set[str] = set(admission.origin_seal_ids)
    if predecessor_session_id is not None and admission.origin_channel_id is not None:
        async with deps.sessionmaker() as db:
            policy = await load_access_policy(db, tenant_id=tenant_id)
        seal |= await inherited_seal_ids(
            deps.anthropic,
            predecessor_session_id=predecessor_session_id,
            own_thread_id=admission.origin_thread_id or admission.origin_channel_id,
            tenant_seals_anything=any_readers_limited(policy),
        )
    if predecessor_session_id is not None:
        async with deps.sessionmaker() as db:
            seal.update(
                await read_session_seals(
                    db, tenant_id=tenant_id, ma_session_id=predecessor_session_id
                )
            )
    if predecessor_session_id is not None:
        # The predecessor lookup above awaits MA: decide again after it, so a
        # pin or seal saved during it applies to the successor being built.
        admission = await reauthorize(deps, admission)
        seal |= admission.origin_seal_ids
    admission = await restrict_inherited_memory(deps, admission, frozenset(seal))
    built = admission

    async def fence() -> None:
        # `create_session` awaits vault, credential, file and repository work
        # before `sessions.create`; decide once more immediately before it.
        # A pin raises `AdmissionDenied`; a seal the session was not built
        # with makes the turn wait and be prepared again read-only.
        current = await reauthorize(deps, built)
        final_seal = set(seal)
        if predecessor_session_id is not None:
            async with deps.sessionmaker() as db:
                final_seal.update(
                    await read_session_seals(
                        db, tenant_id=tenant_id, ma_session_id=predecessor_session_id
                    )
                )
        current = await restrict_inherited_memory(deps, current, frozenset(final_seal))
        if (
            final_seal != seal
            or current.origin_seal_ids != built.origin_seal_ids
            or current.memory_read_only != built.memory_read_only
            or current.asks_before_publishing != built.asks_before_publishing
        ):
            raise SessionBusyError(
                pending_reasons=("seal",),
                retry_after=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1),
            )

    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(admission.agent.id))
    github_mode = "app"
    env_sha256 = await _env_bytes_sha256(deps, tenant_id=tenant_id, agent_uuid=agent_uuid)
    excluded_repo_urls: tuple[str, ...] = ()
    delivered_app_access: AppSessionAccess | None = None

    def capture_app_access(access: AppSessionAccess) -> None:
        nonlocal excluded_repo_urls, delivered_app_access
        excluded_repo_urls = access.excluded_repo_urls
        delivered_app_access = access

    mapping_id = uuid.uuid4()

    async def create(skills: tuple[MaSkill, ...]) -> BetaManagedAgentsSession:
        return await create_session(
            deps.anthropic,
            agent=admission.agent,
            environment=admission.environment,
            mcp_settings=deps.mcp,
            # A shared thread's session carries no caller's credentials.
            account_id=None if admission.shared_owner is not None else admission.account_id,
            # Leaves off every server somebody connected through their own OAuth.
            hide_personal_for=admission.shared_owner,
            tenant_id=tenant_id,
            agent_uuid=agent_uuid,
            session_factory=deps.sessionmaker,
            fernet=deps.fernet,
            github_fallback_pat=deps.github_fallback_pat,
            github_app_id=deps.github_app_id,
            github_app_private_key=deps.github_app_private_key,
            agent_github_app=deps.agent_github_app,
            extra_resources=extra_resources,
            memory_read_only=admission.memory_read_only,
            # A chat session is keyed on the caller's account, so it belongs to one
            # person and may carry that person's own memory.
            personal_memory=True,
            tool_safety=deps.tool_safety,
            legacy_memory_servers=(
                get_repo_memory_settings(deps).legacy_mcp_server_names
                if get_repo_memory_settings(deps).enabled
                else ()
            ),
            asks_before_publishing=admission.asks_before_publishing,
            confirmed_tools=await confirmed_tools_for_tenant(deps, tenant_id),
            daimon_rule_tools=admission.daimon_rule_tools,
            slack_turn_context_id=admission.slack_turn_context_id,
            # A held turn mounts its own channel-bound Daimon credential; a shared
            # thread has no caller account and so no credential to hold.
            hold=None if admission.shared_owner is not None else admission.hold,
            # Held memory is the place's, shared thread or not.
            memory_hold=admission.hold,
            private_dm_id=admission.private_dm_id,
            budget_channel_id=admission.budget_channel_id,
            origin_channel_id=admission.origin_channel_id,
            origin_thread_id=admission.origin_thread_id,
            origin_seal_ids=frozenset(seal),
            before_create=fence,
            on_app_access=capture_app_access,
            channel_skills=skills,
            model_override=admission.model_override,
            system_override=system_for_platform(admission.agent.system, platform),
            relay_session_key=mapping_id,
        )

    while True:
        try:
            ma_session = await create(admission.channel_skills)
            break
        except SkillUnavailable as error:
            refused = error.skill_id
            if refused is None or refused not in admission.platform_skill_ids:
                raise
            # A skill `defaults/` adds on this platform, not one anybody here can
            # remove: run without it, and leave it out of new sessions for a while
            # so the drift check doesn't replace this one on the next turn.
            if deps.platform_skill_memory.refuse(tenant_id, refused, now=dt.datetime.now(dt.UTC)):
                log.warning(
                    "session.platform_skill_refused",
                    tenant_id=str(tenant_id),
                    skill_id=refused,
                    retry_after_s=PLATFORM_SKILL_RETRY_AFTER.total_seconds(),
                    hint="the provider refused a skill defaults/ adds on this platform; "
                    "sessions run without it until it is retried",
                )
            admission = replace(
                admission,
                channel_skills=tuple(
                    skill for skill in admission.channel_skills if skill.skill_id != refused
                ),
                platform_skill_ids=admission.platform_skill_ids - {refused},
            )

    # `create_session` archives its own session on any failure up to its return.
    # From here until `CreatedSession` is returned no row names the session and
    # no caller holds its id, so a cancel (Stop, the ceiling) or a database error
    # in the personal-memory lookup below would leave it live upstream for good.
    try:
        async with deps.sessionmaker() as session:
            personal_memory_store_id = await get_personal_memory_store_id(
                session, tenant_id=tenant_id, agent_id=agent_uuid, account_id=admission.account_id
            )
        has_repo = any(
            isinstance(resource, BetaManagedAgentsGitHubRepositoryResource)
            for resource in ma_session.resources
        )
        # App tokens sit in the vault even when no working repo is mounted; record an
        # issue time only when this session was actually delivered some.
        (
            has_app_tokens,
            token_repo_urls,
            token_scopes,
            token_issued_at_by_installation,
        ) = _delivered_app_snapshot_state(delivered_app_access)
        snapshot = snapshot_from_created_session(
            ma_session,
            env_sha256=env_sha256,
            # Left to the builder: it reads the id off the session's own `.env`
            # file resource, which is the same object `upload_env_and_mount`
            # uploaded a moment ago.
            env_file_id=None,
            # `resolve_clone_token` minted the repo credential inside the
            # `create_session` call above, so "now" is when it was issued.
            repo_token_issued_at=int(time.time()) if has_repo or has_app_tokens else None,
            vault_id=(
                ma_session.vault_ids[-1]
                if github_mode == "app" and ma_session.vault_ids
                else next(iter(ma_session.vault_ids), None)
            ),
            sent_skills=session_skills(admission.agent, admission.channel_skills),
            sent_system=system_for_platform(admission.agent.system, platform),
            github_mode=github_mode,
            personal_memory_store_ids=(
                () if personal_memory_store_id is None else (personal_memory_store_id,)
            ),
            mcp_hold=None if admission.shared_owner is not None else hold_key(admission.hold),
            memory_scope=memory_scope(admission.hold),
        ).model_copy(
            update={
                "token_repo_urls": token_repo_urls,
                "token_scopes": token_scopes,
                "token_issued_at_by_installation": token_issued_at_by_installation,
                "unavailable_repos": tuple(
                    (url, dt.datetime.now(dt.UTC)) for url in excluded_repo_urls
                ),
                "unavailable_notice_pending": excluded_repo_urls,
            }
        )
        return CreatedSession(
            ma_session_id=ma_session.id,
            snapshot=snapshot,
            admission=admission,
            seal_ids=frozenset(seal),
            mapping_id=mapping_id,
        )
    except BaseException:
        from daimon.core.turn.run import (
            _archive_orphaned_session,  # pyright: ignore[reportPrivateUsage]
        )
        from mux.drivers.anthropic import single_attempt_mutation

        try:
            with single_attempt_mutation():
                await _archive_orphaned_session(
                    deps.anthropic,
                    session_id=ma_session.id,
                    scope=admitted_session_scope(
                        admission, tenant_id=tenant_id, session_id=ma_session.id
                    ),
                )
        except Exception as cleanup_error:
            # Preserve the original preparation error if cleanup fails.
            log.warning(
                "turn.recovery_orphan_archive_failed",
                session_id=ma_session.id,
                error=str(cleanup_error)[:200],
            )
        raise


async def insert_mapping(
    db: AsyncSession,
    created: CreatedSession,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    session_account_id: uuid.UUID,
    predecessor_id: uuid.UUID | None = None,
    transfer_file_id: str | None = None,
    transfer_kind: TransferKind | None = None,
) -> FreshSession:
    """Write the `thread_sessions` row for `created` in the caller's transaction.

    The caller commits. Dead-session recovery inserts inside its locked
    transaction so that marking the old row dead, inserting the replacement
    and linking the two commit or roll back together.

    The last three arguments belong to a session that REPLACES another: the
    lineage the new row records about where its work came from.
    """
    snapshot = created.snapshot
    row = await create_thread_session(
        db,
        id=created.mapping_id,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        account_id=session_account_id,
        ma_session_id=created.ma_session_id,
        ma_agent_id=admission.agent.id,
        channel_id=admission.budget_channel_id,
        budget_channel_id=admission.budget_channel_id,
        effective_config=snapshot,
        seal_ids=created.seal_ids,
        identity_fingerprint=fingerprint_identity(snapshot),
        mutable_fingerprint=fingerprint_mutable(snapshot),
        predecessor_id=predecessor_id,
        transfer_file_id=transfer_file_id,
        transfer_kind=transfer_kind,
    )
    return FreshSession(
        ma_session_id=created.ma_session_id,
        mapping_id=row.id,
        snapshot=snapshot,
        admission=created.admission,
    )


async def create_fresh_session(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    session_account_id: uuid.UUID,
    extra_resources: tuple[Resource, ...] = (),
    predecessor_id: uuid.UUID | None = None,
    transfer_file_id: str | None = None,
    transfer_kind: TransferKind | None = None,
) -> FreshSession:
    """Create a brand-new MA session and commit its `thread_sessions` row.

    `create_ma_session` followed by `insert_mapping` in a transaction of its
    own, for callers with no transaction to join (the bind path's no-live-row
    case and a replacement's successor).

    The last four arguments belong to a session that REPLACES another: the
    resources carrying the old session's work, and the lineage the new row
    records about where that work came from. A first session for a thread
    passes none of them.
    """
    predecessor_session_id: str | None = None
    if predecessor_id is not None:
        async with deps.sessionmaker() as session:
            predecessor = await get_thread_session_by_id(session, id=predecessor_id)
        # A lineage row that can't be found still means old work is carried:
        # inherited_seal_ids seals an unreadable predecessor to this thread.
        predecessor_session_id = (
            predecessor.ma_session_id if predecessor is not None else "unknown-predecessor"
        )
    created = await create_ma_session(
        deps,
        admission,
        tenant_id=tenant_id,
        extra_resources=extra_resources,
        predecessor_session_id=predecessor_session_id,
        platform=platform,
    )
    async with deps.sessionmaker() as session:
        fresh = await insert_mapping(
            session,
            created,
            created.admission or admission,
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            session_account_id=session_account_id,
            predecessor_id=predecessor_id,
            transfer_file_id=transfer_file_id,
            transfer_kind=transfer_kind,
        )
        await session.commit()
    return fresh


def bind_recorder(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    external_user_id: str,
    ma_session_id: str,
    model_id: str,
    channel_id: str | None = None,
    agent_id: str | None = None,
) -> UsageRecorder:
    """Build the usage recorder bound to a specific session id and its model.

    `model_id` must be the model THIS SESSION executes — the one frozen in its
    creation-time agent snapshot (`session.agent.model.id`), which is what the
    snapshot on the mapping row records. It is deliberately not the responder
    agent's current model: an `agents.update` never reaches a session that
    already exists, so a session created before a model change keeps running
    the old model, and billing the agent's new one prices work that was never
    done. `headless_runner` already binds `session.agent.model.id` for the same
    reason.

    Factored as a module-level helper (not inlined in `bind_session`) so
    06-05's dead-session recovery cycle can re-invoke it against the NEW
    session id after a recreate, rather than reusing a stale binding.

    `channel_id` is `Admission.budget_channel_id`: the channel whose budget the
    turn's spend counts toward; `agent_id` is the agent that answers, stamped on
    each usage row so spend can be read by agent.
    """
    if (observation := current_outcome.get()) is not None:
        observation.model_by_session[ma_session_id] = model_id
    pricing = MODEL_PRICING.get(model_id)
    if pricing is None:
        # The turn still runs and still records usage; only the debit is zero.
        # Loud, because the operator is giving compute away until it is fixed.
        log.warning("billing.unpriced_model", model_id=model_id, ma_session_id=ma_session_id)
    return functools.partial(
        record_turn_usage,
        sessionmaker=deps.sessionmaker,
        platform_user_id=external_user_id,
        managed_session_id=ma_session_id,
        model_id=model_id,
        tenant_id=tenant_id,
        markup=deps.markup,
        pricing=pricing,
        channel_id=channel_id,
        agent_id=agent_id,
    )


async def bind_session(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    thread_id: str,
    session_account_id: uuid.UUID,
    reuse_existing: bool,
    capabilities: MaCapabilities = DEFAULT_MA_CAPABILITIES,
    transfer: WorkspaceTransfer | None = None,
    deadline: dt.datetime | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    turn_card_intent_id: uuid.UUID | None = None,
) -> PreparedTurn:
    if not isinstance(admission, Admission):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("bind_session requires an Admission instance")
    observation = (
        admission.observation
        or current_outcome.get()
        or TurnObservation(deps.sessionmaker, tenant_id, platform, thread_id=thread_id)
    )
    observation.thread_id = thread_id
    observation.account_id = admission.account_id
    observation.agent_id = admission.agent.id
    try:
        with observation.activate():
            # Any admitted profile may share a thread: a provider preparation
            # receives the shared owner as `session_account_id` and keys its
            # native session by it, with host credentials only.
            shared = await _shared_thread(
                deps, admission, tenant_id=tenant_id, platform=platform, thread_id=thread_id
            )
            if shared is not None:
                # The thread runs under the revision it was bound with (checked
                # runnable by `resolve_shared_thread`), so dispatch picks that
                # provider even after the channel moved to another.
                origin = admission.backend_revision_origin
                if shared.revision != admission.backend_revision:
                    async with deps.sessionmaker() as session:
                        persisted_origin = await mux_state.config_origin(
                            session, shared.revision.channel, shared.revision.local
                        )
                    if persisted_origin not in ("channel", "channel_clear", "agent"):
                        raise AdmissionDenied(reason="backend_unsupported")
                    origin = "agent" if persisted_origin == "agent" else "channel"
                admission = replace(
                    admission,
                    shared_owner=shared.owner,
                    backend_revision=shared.revision,
                    backend_revision_origin=origin,
                )
                session_account_id = shared.owner
            publish: Callable[[PreparedTurn], Awaitable[None]] | None = None
            if (
                deps.backend_selection_enabled
                and admission.private_dm_id is None
                and not (admission.grant is not None and admission.grant.is_dm)
                and admission.config.thread_binding_kind != "setup"
            ):
                # Complete the first binding inside the preparation owner's
                # existing ceiling before exposing it to an awaiting caller.
                from daimon.core.turn.binding import prepared_persistence

                async def _publish(prepared: PreparedTurn) -> None:
                    if shared is not None:
                        async with deps.sessionmaker() as session:
                            await record_shared_binding(
                                session, shared, ma_session_id=prepared.ma_session_id
                            )
                            await session.commit()
                    await prepared_persistence(
                        deps,
                        prepared.admission,
                        tenant_id=tenant_id,
                        platform=platform,
                        thread_id=thread_id,
                        session_id=prepared.ma_session_id,
                        mapping_id=prepared.mapping_id,
                        session_ref=prepared.session_ref,
                        operation_key="preparation",
                    )

                publish = _publish

            bind = bind_session_impl
            if turn_card_intent_id is not None:
                bind = functools.partial(bind_session_impl, turn_card_intent_id=turn_card_intent_id)
            result = await bind(
                deps,
                admission,
                tenant_id=tenant_id,
                platform=platform,
                external_user_id=external_user_id,
                thread_id=thread_id,
                session_account_id=session_account_id,
                reuse_existing=reuse_existing,
                capabilities=capabilities,
                transfer=transfer,
                deadline=deadline,
                now=now,
                _on_prepared=publish,
            )
            if shared is not None and publish is None:
                async with deps.sessionmaker() as session:
                    await record_shared_binding(session, shared, ma_session_id=result.ma_session_id)
                    await session.commit()
    except BaseException as exc:
        observation.finish(error=exc)
        raise
    observation.session_id = result.ma_session_id
    return result


async def _shared_thread(
    deps: TurnDeps, admission: Admission, *, tenant_id: uuid.UUID, platform: str, thread_id: str
) -> SharedThread | None:
    """The thread's shared binding, or None to run per caller as before.

    Reads nothing unless admission carried a channel backend revision that
    can share (`may_have_shared_binding`); a setup conversation and a DM are
    always per caller. A shared thread needs an app-mode agent, whose
    credentials are its own, and a revision that can still run here;
    otherwise the turn is refused visibly.
    """
    revision = admission.backend_revision
    channel_id = admission.origin_channel_id
    if (
        not may_have_shared_binding(revision, ever_shared=admission.backend_ever_shared)
        or channel_id is None
        or admission.private_dm_id is not None
        or admission.config.thread_binding_kind == "setup"
    ):
        return None
    async with deps.sessionmaker() as session:
        try:
            shared = await resolve_shared_thread(
                session,
                revision,
                ever_shared=admission.backend_ever_shared,
                tenant_id=tenant_id,
                platform=platform,
                channel_id=channel_id,
                thread_id=thread_id,
            )
        except BackendUnsupported as err:
            raise AdmissionDenied(reason="backend_unsupported") from err
        if shared is None:
            return None
    return shared


async def _decide_reuse_again(
    deps: TurnDeps, prepared: PreparedTurn, *, now: Callable[[], dt.datetime]
) -> PreparedTurn:
    """Decide the admission again once preparation is done, for a reused session.

    Preparation waits on a lock and may check compatibility before it settles
    on the existing session, so the policy is read once more here: a pin or
    protection added meanwhile refuses the turn (`AdmissionDenied`), and a
    seal added meanwhile is stamped onto the session (`_stamp_reused_seal`).
    A reused session mounted memory writable can't take a new seal in place,
    so that turn waits (`SessionBusyError`) and the next one is prepared
    read-only. A freshly created session was decided in `create_ma_session`.
    """
    if not prepared.reused:
        return prepared
    current = await reauthorize(deps, prepared.admission)
    if current is prepared.admission:
        return prepared
    if (current.memory_read_only and not prepared.admission.memory_read_only) or (
        current.asks_before_publishing and not prepared.admission.asks_before_publishing
    ):
        raise SessionBusyError(
            pending_reasons=("seal",), retry_after=now() + dt.timedelta(seconds=1)
        )
    return replace(prepared, admission=current)


def admitted_session_scope(
    admission: Admission, *, tenant_id: uuid.UUID, session_id: str, prepared: Scope | None = None
) -> Scope:
    """Use the admitted tenant and account without another ownership lookup."""
    if admission.grant is not None and admission.grant.tenant_id != tenant_id:
        raise ScopeViolation(session_id, "the prepared turn belongs to another tenant")
    scope = resource_scope(
        tenant_id=str(tenant_id),
        account_id=str(admission.account_id),
        authorization_id="admitted-turn",
    )
    if prepared is not None:
        if (
            prepared.is_platform
            or prepared.is_legacy_host_authorized
            or (prepared.tenant_id, prepared.account_id) != (scope.tenant_id, scope.account_id)
        ):
            raise ScopeViolation(session_id, "prepared scope differs from admitted identity")
        return prepared
    return scope


async def _stamp_reused_seal(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    tenant_id: uuid.UUID,
    now: Callable[[], dt.datetime],
) -> None:
    """Add this sealed turn's seal to a session it reuses.

    A fresh session is stamped at creation; one opened before its channel was
    sealed lacks this seal and would be readable again after an unseal once
    this turn has written sealed content into it. The recorded seal only
    grows: a thread seal already on the session is kept beside a channel
    one. A session that has gone away is left to the dead-session recovery,
    whose replacement inherits its seal. One MA won't update mid-turn blocks
    this turn: running it unstamped is the leak this exists to close.
    """
    if prepared.reused:
        await stamp_session_seal(
            deps, prepared.ma_session_id, prepared.admission, tenant_id=tenant_id, now=now
        )


async def stamp_session_seal(
    deps: TurnDeps,
    ma_session_id: str,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    now: Callable[[], dt.datetime],
) -> None:
    """Add `admission`'s seal to an existing session's recorded seal (`_stamp_reused_seal`)."""
    if not admission.origin_seal_ids or admission.origin_channel_id is None:
        return
    if _admitted_profile(admission) != "anthropic.managed_agents":
        raise AdmissionDenied(reason="backend_unsupported")
    scope = admitted_session_scope(admission, tenant_id=tenant_id, session_id=ma_session_id)
    # Publish first, in a short transaction; no MA request holds the policy lock.
    async with deps.sessionmaker.begin() as db:
        await record_session_seals(
            db,
            tenant_id=tenant_id,
            ma_session_id=ma_session_id,
            seals=admission.origin_seal_ids,
        )
    try:
        backend = managed_agents(
            deps.anthropic,
            scope=scope,
            resources=frozenset({("session", ma_session_id), ("agent", admission.agent.id)}),
        )
        ref = resource_ref(backend, "session", ma_session_id, scope=scope)
        reads = backend.extension(SessionReads, namespace="anthropic.session_reads", version=1)
        native = await legacy_call(reads.read_native(scope, ref))
        metadata = native.get("metadata")
        recorded = seal_ids(cast("dict[str, str | None] | None", metadata))
        if admission.origin_seal_ids <= recorded:
            return
        revision = Revision(local=0)
        desired = SessionSpec(
            agent=resource_ref(backend, "agent", admission.agent.id, scope=scope),
            agent_revision=revision,
            config_revision=0,
            metadata=dict(
                origin_stamp(
                    channel_id=admission.origin_channel_id,
                    thread_id=admission.origin_thread_id,
                    seal=recorded | admission.origin_seal_ids,
                )
            ),
        )
        plan = await backend.sessions.plan_update(scope, ref, desired)
        await legacy_call(
            backend.sessions.apply_update(scope, plan, expected=revision, key=str(uuid.uuid4()))
        )
    except anthropic_pkg.NotFoundError:
        log.info("turn.seal_stamp_session_gone", ma_session_id=ma_session_id)
    except anthropic_pkg.APIStatusError as error:
        # MA refuses updates mid-turn ("... while session is running"); any
        # other refusal fails the turn rather than running it unstamped.
        if error.status_code != 409 and "while session is running" not in str(error):
            raise
        log.info("turn.seal_stamp_busy", ma_session_id=ma_session_id)
        raise SessionBusyError(
            pending_reasons=("seal",), retry_after=now() + dt.timedelta(seconds=5)
        ) from error


async def bind_session_impl(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    external_user_id: str,
    thread_id: str,
    session_account_id: uuid.UUID,
    reuse_existing: bool,
    capabilities: MaCapabilities = DEFAULT_MA_CAPABILITIES,
    transfer: WorkspaceTransfer | None = None,
    deadline: dt.datetime | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    _on_prepared: Callable[[PreparedTurn], Awaitable[None]] | None = None,
    _on_orphan: Callable[[PreparedTurn], Awaitable[None]] | None = None,
    turn_card_intent_id: uuid.UUID | None = None,
) -> PreparedTurn:
    """Find-or-create the MA session for this turn and bind its recorder.

    A thin wrapper over `prepare_session_for_turn` for callers that want a
    `PreparedTurn` or an exception. A deferred preparation returns the turn it
    prepared against the current session — the change lands at the caller's
    next message, and `prepared.continuity.pending` says which — while a failed
    one raises `SessionPreparationFailed`, because the turn must not run
    against a configuration nobody asked for. A busy one raises
    `SessionBusyError` for the same reason with a different cause: the session
    still in flight belongs to the responder being replaced, so there is no
    session this turn could honestly run on yet.

    When `reuse_existing` is True, a live `thread_sessions` row for
    (tenant_id, platform, thread_id, session_account_id) is reused, refreshed
    in place, or replaced according to how far its frozen configuration has
    drifted from the caller's. Otherwise (no live row, or
    `reuse_existing=False` for Discord's channel-mention path) a fresh
    session is created via the single shared `create_session` call site,
    always passing `fernet=deps.fernet`, and a new `thread_sessions` mapping
    row is written.

    Billing binds to the model the bound session actually runs: the fresh
    session's own snapshot, or the reused row's recorded one (backfilled from
    MA for a row written before snapshots existed). The responder agent's
    current model is only a fallback for a session we cannot read.

    `deadline`/`now` bound this whole body against the per-turn ceiling:
    the MA `sessions.create` call and the mapping write, and on the
    reuse path the compatibility check and whatever it applies. This does NOT
    cover `admit()`, which runs before `bind_session` and is deliberately
    unbounded. `deadline=None` is fail-safe, not off -- it computes
    `turn_deadline(now=now())` so every caller (including one that never
    passes a deadline) is still ceiling-covered.

    Raises `TypeError` if `admission` is not a real `Admission` -- pyright's
    strict mode already rejects a mistyped caller at type-check time; this
    guard makes the same contract hold at runtime (the type-level chokepoint
    claim tested by 06-05's `test_bind_session_requires_an_admission_value`).
    This check runs BEFORE the ceiling wrap, so a mistyped caller still fails
    immediately with the same error rather than waiting out a timeout.
    """
    if not isinstance(admission, Admission):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(
            f"bind_session requires an Admission instance, got {type(admission).__name__}"
        )

    effective_deadline = deadline if deadline is not None else turn_deadline(now=now())

    profile = _admitted_profile(admission)
    scope = admitted_session_scope(admission, tenant_id=tenant_id, session_id="preparation")
    request = ProviderPreparationRequest(
        deps,
        admission,
        scope,
        tenant_id,
        platform,
        external_user_id,
        thread_id,
        session_account_id,
        reuse_existing,
        capabilities,
        transfer,
        effective_deadline,
        now,
    )
    deps, effective_deadline = request.deps, request.deadline

    async def _complete(preparing: Awaitable[PreparedTurn]) -> PreparedTurn:
        result = await preparing
        try:
            if _on_prepared is not None:
                await _on_prepared(result)
            return result
        except BaseException:
            raise

    if profile != "anthropic.managed_agents":
        if (deps.turn_path or load_turn_settings().path) != "mux":
            raise AdmissionDenied(reason="backend_unsupported")
        from daimon.core.turn.runtime_manifest import fixed_plan_origin_allowed

        if not fixed_plan_origin_allowed(admission.backend_revision_origin) and not (
            admission.backend_revision_origin == "agent"
            and deps.agent_backends
            and deps.channel_backends
        ):
            raise AdmissionDenied(reason="backend_unsupported")

        prepare = _PROVIDER_PREPARATIONS.get(profile)
        if prepare is None and ensure_channel_runtime(
            profile, ready=lambda: profile in _PROVIDER_PREPARATIONS
        ):
            prepare = _PROVIDER_PREPARATIONS.get(profile)
        if prepare is None:
            raise AdmissionDenied(reason="backend_unsupported")
        request, runtimes = scope_channel_runtimes(request)

        async def _prepare_provider() -> PreparedTurn:
            selected = request
            if admission.backend_revision_origin == "agent":
                from daimon.core.turn.runtime_manifest import read_preparation_counterpart

                selected = await read_preparation_counterpart(request)
            result = await prepare(selected)
            # Unattended execution must inspect actual provider tools, not the
            # source's Anthropic representation or a retained origin grant.
            native_spec = (
                selected.verified_counterpart.agent.spec
                if selected.verified_counterpart is not None
                else selected.target_spec
            )
            result = replace(result, _native_spec=native_spec)
            ref = result.session_ref
            backend = result.backend
            if ref is None or (
                _admitted_profile(result.admission) != profile
                or result.admission.backend_revision != admission.backend_revision
                or result.admission.backend_revision_origin != admission.backend_revision_origin
                or (backend is not None and backend.capabilities().profile_id != profile)
                or ref.provider != get_profile(profile).provider
                or ref.id != result.ma_session_id
                or ref.kind != "session"
                or ref.tenant_id != scope.tenant_id
                or ref.account_id != scope.account_id
                or result.admission.account_id != admission.account_id
                or result.session_account_id != session_account_id
            ):
                raise ScopeViolation("preparation", "provider returned a foreign prepared session")
            if runtimes is not None:
                result = replace(result, runtime=result.runtime or runtimes.resolved)
            return result

        try:
            timeout = remaining_s(effective_deadline, now=now(), refuse_expired=False)
            return await asyncio.wait_for(_complete(_prepare_provider()), timeout=timeout)
        except TimeoutError as error:
            raise ceiling_error() from error

    async def _bind() -> PreparedTurn:
        # Decide the admission again at the moment its session is found,
        # reused, replaced or created: a pin, seal, protection or invoker
        # change since `admit()` applies to this turn, and a seal added in
        # between is stamped on the session (`reauthorize`).
        current = await reauthorize(deps, admission)
        # Imported here, not at module scope: `session_preparation` imports
        # this module for the types above, and importing it back at module
        # scope would make either module unimportable first. The import is
        # cached after the first bind.
        from daimon.core.session_preparation import (
            PreparationBusy,
            PreparationDeferred,
            PreparationFailure,
            SessionOps,
            prepare_session_for_turn,
        )
        from daimon.core.workspace_transfer import WorkspaceTransferRunner

        # Chat callers get the checkpoint-and-bundle transfer by default; a
        # caller that must never spend a checkpoint turn passes its own hook.
        effective_transfer = transfer or WorkspaceTransferRunner(
            anthropic=deps.anthropic,
            sessionmaker=deps.sessionmaker,
            tenant_id=tenant_id,
            fernet=deps.fernet,
            account_id=current.account_id,
            external_user_id=external_user_id,
            markup=deps.markup,
            tool_safety=deps.tool_safety,
            public_url=deps.public_url,
            platform=platform,
            channel_id=admission.budget_channel_id,
            turn_path=deps.turn_path,
        )

        # `SessionOps` is built here rather than at import time so that
        # `create_session` and the store helpers resolve from this module's
        # globals when the bind runs, which is what keeps them patchable.
        outcome = await prepare_session_for_turn(
            deps,
            current,
            ops=SessionOps(
                read_live_row=get_live_thread_session,
                create_fresh=create_fresh_session,
                bind_record=bind_recorder,
            ),
            tenant_id=tenant_id,
            platform=platform,
            external_user_id=external_user_id,
            thread_id=thread_id,
            session_account_id=session_account_id,
            reuse_existing=reuse_existing,
            capabilities=capabilities,
            transfer=effective_transfer,
            deadline=effective_deadline,
            now=now,
        )
        if isinstance(outcome, PreparationDeferred):
            prepared_turn = await _decide_reuse_again(deps, outcome.prepared, now=now)
            await _stamp_reused_seal(deps, prepared_turn, tenant_id=tenant_id, now=now)
            return prepared_turn
        if isinstance(outcome, PreparationBusy):
            raise SessionBusyError(
                pending_reasons=outcome.pending_reasons, retry_after=outcome.retry_after
            )
        if isinstance(outcome, PreparationFailure):
            raise SessionPreparationFailed(
                reasons=outcome.reasons,
                stage=outcome.stage,
                retry_after=outcome.retry_after,
                preserved=outcome.preserved,
            )
        prepared_turn = await _decide_reuse_again(deps, outcome, now=now)
        await _stamp_reused_seal(deps, prepared_turn, tenant_id=tenant_id, now=now)
        return prepared_turn

    try:
        return await asyncio.wait_for(
            _complete(_bind()), timeout=remaining_s(effective_deadline, now=now())
        )
    except TimeoutError as err:
        log.error(
            "turn.ceiling_exceeded",
            phase="bind_session",
            tenant_id=str(tenant_id),
            platform=platform,
            thread_id=thread_id,
            deadline=effective_deadline.isoformat(),
        )
        raise ceiling_error() from err

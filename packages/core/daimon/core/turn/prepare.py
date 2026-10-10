"""Stage two of the two-stage turn chokepoint (D-01): `bind_session()`.

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
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import anthropic as anthropic_pkg
import structlog
from anthropic.types.beta.beta_managed_agents_system_content_block_param import (
    BetaManagedAgentsSystemContentBlockParam,
)
from anthropic.types.beta.session_create_params import Resource
from anthropic.types.beta.sessions.beta_managed_agents_github_repository_resource import (
    BetaManagedAgentsGitHubRepositoryResource,
)
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.permissions import any_readers_limited
from daimon.core.pricing import MODEL_PRICING
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES, ChangeReason, MaCapabilities
from daimon.core.session_seal import inherited_seal_ids, origin_stamp, seal_ids
from daimon.core.session_snapshot import (
    SessionSnapshot,
    fingerprint_identity,
    fingerprint_mutable,
    hash_env_bytes,
    session_skills,
    snapshot_from_created_session,
)
from daimon.core.sessions import create_session
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.domain import TransferKind
from daimon.core.stores.github_access import get_agent_mode
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
from daimon.core.turn.errors import SessionBusyError, SessionPreparationFailed
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.posture import UsageRecorder
from daimon.core.turn_origin import SessionState
from daimon.core.usage_recording import record_turn_usage
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from daimon.core.session_preparation_stages import WorkspaceTransfer

log = structlog.get_logger(__name__)


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
    _record: UsageRecorder
    continuity: ContinuityOutcome = CONTINUED


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
        mode = await get_agent_mode(session, tenant_id=tenant_id, agent_id=agent_uuid)
    if mode == "app":
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


async def create_ma_session(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    extra_resources: tuple[Resource, ...] = (),
    predecessor_session_id: str | None = None,
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
    """
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
    async with deps.sessionmaker() as session:
        github_mode = await get_agent_mode(session, tenant_id=tenant_id, agent_id=agent_uuid)
    env_sha256 = await _env_bytes_sha256(deps, tenant_id=tenant_id, agent_uuid=agent_uuid)
    ma_session = await create_session(
        deps.anthropic,
        agent=admission.agent,
        environment=admission.environment,
        mcp_settings=deps.mcp,
        account_id=admission.account_id,
        tenant_id=tenant_id,
        agent_uuid=agent_uuid,
        session_factory=deps.sessionmaker,
        fernet=deps.fernet,
        github_fallback_pat=deps.github_fallback_pat,
        github_app_id=deps.github_app_id,
        github_app_private_key=deps.github_app_private_key,
        agent_github_app=deps.agent_github_app,
        is_external=admission.is_external,
        extra_resources=extra_resources,
        memory_read_only=admission.memory_read_only,
        tool_safety=deps.tool_safety,
        asks_before_publishing=admission.asks_before_publishing,
        slack_turn_context_id=admission.slack_turn_context_id,
        private_dm_id=admission.private_dm_id,
        budget_channel_id=admission.budget_channel_id,
        origin_channel_id=admission.origin_channel_id,
        origin_thread_id=admission.origin_thread_id,
        origin_seal_ids=frozenset(seal),
        before_create=fence,
        channel_skills=admission.channel_skills,
    )

    has_repo = any(
        isinstance(resource, BetaManagedAgentsGitHubRepositoryResource)
        for resource in ma_session.resources
    )
    snapshot = snapshot_from_created_session(
        ma_session,
        env_sha256=env_sha256,
        # Left to the builder: it reads the id off the session's own `.env`
        # file resource, which is the same object `upload_env_and_mount`
        # uploaded a moment ago.
        env_file_id=None,
        # `resolve_clone_token` minted the repo credential inside the
        # `create_session` call above, so "now" is when it was issued.
        repo_token_issued_at=int(time.time()) if has_repo or github_mode == "app" else None,
        vault_id=(
            ma_session.vault_ids[-1]
            if github_mode == "app" and ma_session.vault_ids
            else next(iter(ma_session.vault_ids), None)
        ),
        sent_skills=session_skills(admission.agent, admission.channel_skills),
        github_mode=github_mode,
    )
    return CreatedSession(
        ma_session_id=ma_session.id,
        snapshot=snapshot,
        admission=admission,
        seal_ids=frozenset(seal),
    )


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
        tenant_id=tenant_id,
        platform=platform,
        thread_id=thread_id,
        account_id=session_account_id,
        ma_session_id=created.ma_session_id,
        ma_agent_id=admission.agent.id,
        channel_id=admission.budget_channel_id,
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
    turn's spend counts toward.
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
            result = await bind_session_impl(
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
            )
    except BaseException as exc:
        observation.finish(error=exc)
        raise
    observation.session_id = result.ma_session_id
    return result


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


async def _stamp_reused_seal(
    deps: TurnDeps, prepared: PreparedTurn, *, now: Callable[[], dt.datetime]
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
        await stamp_session_seal(deps, prepared.ma_session_id, prepared.admission, now=now)


async def stamp_session_seal(
    deps: TurnDeps, ma_session_id: str, admission: Admission, *, now: Callable[[], dt.datetime]
) -> None:
    """Add `admission`'s seal to an existing session's recorded seal (`_stamp_reused_seal`)."""
    if not admission.origin_seal_ids or admission.origin_channel_id is None:
        return
    # Publish first, in a short transaction; no MA request holds the policy lock.
    async with deps.sessionmaker.begin() as db:
        await record_session_seals(
            db,
            tenant_id=admission.grant.tenant_id if admission.grant is not None else None,
            ma_session_id=ma_session_id,
            seals=admission.origin_seal_ids,
        )
    try:
        current = await deps.anthropic.beta.sessions.retrieve(ma_session_id)
        recorded = seal_ids(current.metadata)
        if admission.origin_seal_ids <= recorded:
            return
        await deps.anthropic.beta.sessions.update(
            ma_session_id,
            metadata=dict(
                origin_stamp(
                    channel_id=admission.origin_channel_id,
                    thread_id=admission.origin_thread_id,
                    seal=recorded | admission.origin_seal_ids,
                )
            ),
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

    `deadline`/`now` bound this whole body against the per-turn ceiling
    (D-03): the MA `sessions.create` call and the mapping write, and on the
    reuse path the compatibility check and whatever it applies. This does NOT
    cover `admit()`, which runs before `bind_session` and is deliberately
    unbounded (D-04). `deadline=None` is fail-safe, not off -- it computes
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
            external_user_id=external_user_id,
            markup=deps.markup,
            channel_id=admission.budget_channel_id,
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
            await _stamp_reused_seal(deps, prepared_turn, now=now)
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
        await _stamp_reused_seal(deps, prepared_turn, now=now)
        return prepared_turn

    try:
        return await asyncio.wait_for(_bind(), timeout=remaining_s(effective_deadline, now=now()))
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

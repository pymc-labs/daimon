"""Non-interactive turn execution.

Open a fresh MA session, then delegate the drain to the core turn driver
(`daimon.core.turn.driver.run_turn`), returning the complete final message.
A routine turn inherits the driver's full liveness story — status-checked
eventless-cycle reconnect, the per-call read timeout, the cancel race,
the hardened render error policy — instead of a bespoke bare drain
loop, and answers tool confirmations via `headless_tool_confirmation`.

A routine/headless turn is bounded end to end by the core per-turn ceiling
(`daimon.core.turn.ceiling`, ~45 minutes): one shared deadline covers BOTH
session assembly (the `agent`/`environment` retrieve calls plus
`create_session`) and the delegated drain, mirroring how the chat path
splits its own ceiling coverage between `bind_session` and
`run_prepared_turn`. `deadline` is fail-safe like those two sites: a caller
that passes none (every caller today, including the scheduler's `_fire`)
still gets `turn_deadline(now=datetime.now(UTC))` computed here. A breach
during assembly is raised as `TurnError(kind="ceiling")`; a breach during
the drain comes back the same way via the driver's own `state.error` and
this function's existing `if state.error is not None: raise state.error`.
Either way it reaches the scheduler's `_fire_guarded` boundary as
`"TurnError: ceiling: ..."` via `_record_fire_error`, not the adapter's own
bespoke `"timeout: exceeded Ns"` string — the scheduler's `dispatch_timeout_s`
is now strictly above `TURN_CEILING_S` and exists only as an outer process
guard that this ceiling always fires ahead of.

Used by `daimon.adapters.scheduler` for routine fires and by
`daimon.core.smoke` for the post-deploy smoke turn.

Session assembly is delegated to `create_session` in `daimon.core.sessions`
(the same collapse that unified the MCP `start_turn` path) — this is the
single source of truth for vault/PAT/env-mount/repo-resource assembly AND
the `daimon_tenant`/`daimon_account` metadata stamp that
`daimon.core.usage_sweep.session_billing` requires to bill a session.
`run_turn` keeps its string `agent_id`/`environment_id` signature (the
scheduler only has ids) and bridges to `create_session`'s SDK-object
signature via neutral agent/environment retrieval ports and the M0 SDK codecs
(mirrors `daimon.adapters.cli.sessions_bootstrap`). An optional
`model_override` applies only to the new session; it never updates the stored
agent.

Per `guideline:architecture` "Error propagation" the runner does not
swallow exceptions: `httpx.HTTPError`, `anthropic.APIError`, and a failed
turn's `TurnState.error` (raised as-is, a `TurnError`) all propagate to the
caller (the scheduler's `_fire_one`, which is the boundary).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from cryptography.fernet import MultiFernet
from daimon.core.channel_backend import resolve_turn_backend
from daimon.core.channel_budget import admit_fleet_worker, require_fleet_worker_admission
from daimon.core.config import GithubAppSettings, McpSettings, load_turn_settings
from daimon.core.context_prompt import TurnContext, context_prompt
from daimon.core.continuity.wakes import enqueue_worker_finished
from daimon.core.fleet import TERMINAL, FleetConflict, FleetWorker, spawn_operation_key, worker_slot
from daimon.core.github_app_session import close_headless_app_session
from daimon.core.model_presets import model_choice_id
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import legacy_call, sdk_agent, sdk_environment
from daimon.core.neutral_resources import WorkerPermissions, resolve_worker_spec
from daimon.core.platform_system import system_for_platform
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.rule_views import RoutineOrigin
from daimon.core.sessions import create_session
from daimon.core.stores import access_policy as policy_store
from daimon.core.stores import fleet as fleet_store
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.mux_state import PostgresStateStore, database_clock
from daimon.core.tool_safety import (
    OPEN_TOOL_SAFETY,
    ToolSafetyPolicy,
    daimon_endpoint,
    trusted_servers_for,
)
from daimon.core.turn.approvals import (
    agent_rule_lookup,
    daimon_rule_tools_for,
    headless_tool_confirmation,
)
from daimon.core.turn.binding import prepared_persistence
from daimon.core.turn.ceiling import ceiling_error, remaining_s, turn_deadline
from daimon.core.turn.driver import run_turn as drive_turn
from daimon.core.turn.io import turn_io
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.persistence import MutationRetry, TurnPersistence, current_persistence
from daimon.core.turn.posture import (
    Billed,
    BillingExempt,
    BillingPosture,
    ExemptReason,
)
from daimon.core.turn.prepare import (
    PreparedTurn,
    ProviderPreparationRequest,
    bind_session_impl,
    clone_overrides,
)
from daimon.core.turn.run import (
    _archive_orphaned_session,  # pyright: ignore[reportPrivateUsage]
    _turn_port_kwargs,  # pyright: ignore[reportPrivateUsage]
    _with_prefix,  # pyright: ignore[reportPrivateUsage]
    run_prepared_turn,
)
from daimon.core.turn.runtime_manifest import capture_preparation_source
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.turn.termination import TerminationReason
from daimon.core.turn_hold import TurnHold
from mux.contracts.actions import UserMessage
from mux.contracts.config import BackendConfig
from mux.contracts.events import TextPart
from mux.contracts.ids import ChannelRef, Scope, ThreadRef
from mux.contracts.resources import Agent, AgentSpec, ProviderBinding
from mux.errors import ScopeViolation
from mux.profiles import MANAGED_AGENTS
from mux.state.lease import Lease, Slot
from mux.state.memory import MemoryStateStore
from mux.state.mutation_retry import MutationRetryPolicy
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.fleet_orchestration import WorkerFailureCode

log = structlog.get_logger(__name__)

TurnPath = Literal["legacy", "mux"]
"""The two turn paths (`TurnSettings.path`), named so a caller can pass one in."""


class _NoOpLifecycle(TurnLifecycle):
    """A headless turn has no surface to render to, so every delivery hook
    is a no-op. Inherits `TurnLifecycle`'s own default no-op bodies for the
    four optional hooks (`on_sse_event`/`on_reconnect`/`on_rate_limited`/
    `on_interrupt_sent`); only the three mandatory ones are overridden here.

    Failures are NOT raised from here. `run_turn` (below) inspects
    `TurnState.error` after the driver's `run_turn` returns and raises then —
    the driver's own finalizer/render machinery deliberately folds failures
    into `TurnState.error` rather than raising (RESEARCH.md Open Question 2),
    and a lifecycle hook that raised would fight that design from inside the
    driver's own pump.
    """

    def __init__(self, *, on_result: Callable[[TurnState], Awaitable[None]] | None = None) -> None:
        self._on_result = on_result

    async def on_render(self, state: TurnState) -> None:
        return None

    async def on_terminal_success(self, state: TurnState) -> None:
        if self._on_result is not None and state.termination == TerminationReason.COMPLETED:
            await self._on_result(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        return None


def _assert_satisfies_turn_lifecycle(lifecycle: TurnLifecycle) -> None:
    """Static-typing guard: `_NoOpLifecycle` satisfies `TurnLifecycle`.

    Runtime no-op; pyright's structural check on the parameter type is what
    matters here, mirroring `daimon.testing.turn_fakes.assert_lifecycle`.
    """
    del lifecycle


_assert_satisfies_turn_lifecycle(_NoOpLifecycle())


async def run_turn(
    *,
    anthropic: AsyncAnthropic,
    agent_id: str,
    environment_id: str,
    trigger_message: str,
    origin: TurnContext = "routine",
    mcp_settings: McpSettings | None = None,
    account_id: uuid.UUID | None = None,
    usage_record_factory: Callable[[str, str], Callable[..., Awaitable[object]]] | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_uuid: uuid.UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    fernet: MultiFernet | None = None,
    github_fallback_pat: str | None = None,
    github_app_id: str | None = None,
    github_app_private_key: str | None = None,
    agent_github_app: GithubAppSettings | None = None,
    deadline: datetime | None = None,
    tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY,
    on_state: Callable[[TurnState], None] | None = None,
    budget_channel_id: str | None = None,
    origin_place: RoutineOrigin | None = None,
    model_override: str | None = None,
    personal_memory: bool = False,
    platform: str | None = None,
    hold: TurnHold | None = None,
    turn_path: TurnPath | None = None,
) -> str:
    observation = current_outcome.get()
    owns_observation = observation is None
    if observation is None and session_factory is not None:
        observation = TurnObservation(session_factory, tenant_id, "headless", origin=origin)
    if observation is None:
        return await run_turn_impl(
            anthropic=anthropic,
            agent_id=agent_id,
            environment_id=environment_id,
            trigger_message=trigger_message,
            origin=origin,
            mcp_settings=mcp_settings,
            account_id=account_id,
            usage_record_factory=usage_record_factory,
            tenant_id=tenant_id,
            agent_uuid=agent_uuid,
            session_factory=session_factory,
            fernet=fernet,
            github_fallback_pat=github_fallback_pat,
            github_app_id=github_app_id,
            github_app_private_key=github_app_private_key,
            agent_github_app=agent_github_app,
            deadline=deadline,
            tool_safety=tool_safety,
            on_state=on_state,
            budget_channel_id=budget_channel_id,
            origin_place=origin_place,
            model_override=model_override,
            personal_memory=personal_memory,
            platform=platform,
            hold=hold,
            turn_path=turn_path,
        )
    observation.agent_id = agent_id
    observation.account_id = account_id
    observation.origin = origin
    try:
        with observation.activate():
            result = await run_turn_impl(
                anthropic=anthropic,
                agent_id=agent_id,
                environment_id=environment_id,
                trigger_message=trigger_message,
                origin=origin,
                mcp_settings=mcp_settings,
                account_id=account_id,
                usage_record_factory=usage_record_factory,
                tenant_id=tenant_id,
                agent_uuid=agent_uuid,
                session_factory=session_factory,
                fernet=fernet,
                github_fallback_pat=github_fallback_pat,
                github_app_id=github_app_id,
                github_app_private_key=github_app_private_key,
                agent_github_app=agent_github_app,
                deadline=deadline,
                tool_safety=tool_safety,
                on_state=on_state,
                budget_channel_id=budget_channel_id,
                origin_place=origin_place,
                model_override=model_override,
                personal_memory=personal_memory,
                platform=platform,
                hold=hold,
                turn_path=turn_path,
            )
    except BaseException as exc:
        # The enclosing scheduler owns its deadline and classifies wait_for cancellation.
        if owns_observation or not isinstance(exc, asyncio.CancelledError):
            observation.finish(error=exc)
        raise
    return result


def _outside_turn_persistence[**P, R](run: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Run a headless turn without the persistence of whichever turn started it.

    A headless run started from inside another turn (a worker allocation, a
    tool call) inherits that turn's `current_persistence` through the
    context. On mux the driver binds it, and `check_session` then refuses the
    headless run's own session as one the outer turn does not own. A
    headless run owns nothing the outer turn does, so it starts clean and
    binds its own session after assembly. The fleet worker's own persistence
    is activated by `run_prepared_turn` and read inside that run
    (`run_worker`'s `publish`), not here.
    """

    @functools.wraps(run)
    async def isolated(*args: P.args, **kwargs: P.kwargs) -> R:
        token = current_persistence.set(None)
        try:
            return await run(*args, **kwargs)
        finally:
            current_persistence.reset(token)

    return isolated


@_outside_turn_persistence
async def run_turn_impl(
    *,
    anthropic: AsyncAnthropic,
    agent_id: str,
    environment_id: str,
    trigger_message: str,
    origin: TurnContext = "routine",
    mcp_settings: McpSettings | None = None,
    account_id: uuid.UUID | None = None,
    usage_record_factory: Callable[[str, str], Callable[..., Awaitable[object]]] | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_uuid: uuid.UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    fernet: MultiFernet | None = None,
    github_fallback_pat: str | None = None,
    github_app_id: str | None = None,
    github_app_private_key: str | None = None,
    agent_github_app: GithubAppSettings | None = None,
    deadline: datetime | None = None,
    tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY,
    on_state: Callable[[TurnState], None] | None = None,
    budget_channel_id: str | None = None,
    origin_place: RoutineOrigin | None = None,
    model_override: str | None = None,
    personal_memory: bool = False,
    platform: str | None = None,
    hold: TurnHold | None = None,
    turn_path: TurnPath | None = None,
) -> str:
    """Run a single non-interactive turn end-to-end and return its complete final reply.

    Flow:

    1. Neutral agent/environment retrieval ports
       resolve the SDK objects ``create_session`` needs (it takes objects, not
       ids — the scheduler only has ids after `resolve_agent`/`resolve_environment`).
    2. ``create_session(...)`` (from ``daimon.core.sessions``) opens a fresh MA session.
       When ``mcp_settings`` carries both ``public_url`` and ``jwt_secret``,
       ``ensure_agent_mcp_vault`` runs first and the per-agent vault id is
       attached. Both ``account_id`` and ``agent_uuid`` are required in that
       case; missing either raises ``ValueError`` (no fallback to an
       account-scoped vault). ``create_session`` also stamps the
       session's ``metadata`` with ``daimon_tenant``/``daimon_account`` when
       given, which is what ``usage_sweep.session_billing`` bills the
       session by. The repo binding is
       fetched unconditionally inside ``create_session`` — the operator
       ``github_fallback_pat`` clones ``anon:`` (verified-public) bindings
       even without a per-agent PAT.
    3. The drain is delegated to ``daimon.core.turn.driver.run_turn`` with a
       no-op lifecycle (nothing to render to), ``tool_confirmation`` from
       ``headless_tool_confirmation(tool_safety)`` (``AutoApprove`` while the
       tool-safety policy is off; with it on, reads run and third-party
       writes are refused unless the operator allowed them in unattended
       runs — nobody is here to press Approve), ``deadline=effective_deadline``
       (the same instant that bounded step 1/2's assembly, so the driver's own
       ``remaining_s`` naturally subtracts the time assembly already spent —
       one window, two disjoint legs, no double enforcement), and a billing
       posture of ``Billed(record=usage_record)`` when a
       ``usage_record_factory`` was given, else
       ``BillingExempt(reason="headless-unrecorded")``. The exempt case is
       also stamped on the session at step 2 (``daimon_billing_exempt``), so
       the usage sweep skips it rather than debiting the tenant. A routine turn now
       inherits the driver's full liveness story — status-checked
       eventless-cycle reconnect, the per-call read timeout, the cancel race,
       the hardened render error policy — instead of a bespoke bare drain
       loop with no reconnect and stream-end-as-success.
    4. After the driver returns: ``extract_final_response(state.content)``.
       ``on_state``, when given, is called first with the final ``TurnState``
       of a successful turn, for a caller that needs the tool trail too (the
       scheduler checks whether the agent posted a routine's result itself).

    Errors:

    - A failed turn surfaces as the driver's own ``TurnState.error`` (a
      ``TurnError``), raised as-is here rather than wrapped — the scheduler's
      boundary (``_fire_one``) still catches it as a hard failure and records
      ``last_error``. The message shape changes from the old bespoke
      ``"session.error: ..."`` to the driver's own kind-prefixed wording
      (e.g. ``"upstream: ..."``, ``"ceiling: ..."``), which is strictly more
      informative.
    - ``httpx.HTTPError`` and ``anthropic.APIError`` propagate uncaught (the
      driver folds most of these into ``TurnState.error`` rather than
      raising directly; only failures the driver itself cannot recover from,
      like a billing recorder's own exception, escape as raw exceptions).

    ``usage_record_factory``, if provided, is invoked once after the MA
    session opens with ``(session.id, session.agent.model.id)`` and must
    return the bound ``usage_record`` callable. The returned callable is
    awaited for each ``span.model_request_end`` event with kwarg
    ``event=event`` (no ``session_id`` — the recorder binds session/tenant
    context itself via ``functools.partial``). The factory shape
    exists because ``model_id`` is only known after ``create_session``
    returns, but adapter callers want to preset their own routine context
    (platform, user, guild) via ``functools.partial`` before fire time.

    ``budget_channel_id`` is stamped on the session as ``daimon_budget_channel``
    so the usage sweep attributes any spend it replays to that channel's budget.
    ``origin_place`` stamps where the routine fires (`routine_origin`), so its
    transcript reads only where a turn there would, and only by its owner.

    ``personal_memory`` also mounts ``account_id``'s personal store, with the
    same (read-only, for a routine) access as the shared one. The scheduler
    sets it only for a routine whose result reaches nobody but its owner,
    decided again at every fire (`rule_views.routine_answers_only_owner`).

    ``hold`` runs the turn held to a shared channel (`routine_hold.routine_hold`):
    the channel's own store replaces the agent-wide and personal ones, and the
    Daimon credential is the held one.

    ``turn_path`` is the turn path this fire runs on, resolved by the caller at
    its edge (``load_turn_settings().path``). It is read once here and handed to
    the driver, so nothing below re-reads settings for the turn. ``None`` falls
    back to one settings read here.
    """
    turn_scope: Scope | None = None
    selected_path = turn_path if turn_path is not None else load_turn_settings().path
    if selected_path == "mux":
        if tenant_id is None or account_id is None:
            raise ScopeViolation(
                agent_id, "mux headless turns require the authorized tenant/account"
            )
        turn_scope = resource_scope(
            tenant_id=str(tenant_id),
            account_id=str(account_id),
            authorization_id="headless-turn",
        )

    effective_deadline = deadline if deadline is not None else turn_deadline(now=datetime.now(UTC))
    # Decided before the session exists so the session carries it: an
    # unrecorded run is stamped exempt, and the usage sweep then skips it
    # instead of debiting the tenant for a turn nobody meant to bill.
    billing_exempt: ExemptReason | None = (
        "headless-unrecorded" if usage_record_factory is None else None
    )

    # Read once for the session's tools and for the run's decider; the agent's
    # id is the one its rules are keyed by.
    daimon_rule_tools = (
        await daimon_rule_tools_for(
            session_factory, tool_safety, tenant_id=tenant_id, agent_id=agent_id
        )
        if session_factory is not None
        else frozenset[str]()
    )

    async def _assemble_session() -> BetaManagedAgentsSession:
        scope = (
            resource_scope(
                tenant_id=str(tenant_id),
                account_id=str(account_id) if account_id is not None else "service",
                authorization_id="headless-session-assembly",
            )
            if tenant_id is not None
            else Scope.legacy_host_authorized(call_site="headless_runner:run_turn:tenantless")
        )
        backend = managed_agents(
            anthropic,
            scope=scope,
            resources=frozenset({("agent", agent_id), ("environment", environment_id)}),
        )
        agent = sdk_agent(
            await legacy_call(
                backend.agents.retrieve(
                    scope, resource_ref(backend, "agent", agent_id, scope=scope)
                )
            )
        )
        environment = sdk_environment(
            await legacy_call(
                backend.environments.retrieve(
                    scope, resource_ref(backend, "environment", environment_id, scope=scope)
                )
            )
        )
        return await create_session(
            anthropic,
            agent=agent,
            environment=environment,
            mcp_settings=mcp_settings,
            daimon_rule_tools=daimon_rule_tools,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_uuid=agent_uuid,
            session_factory=session_factory,
            fernet=fernet,
            github_fallback_pat=github_fallback_pat,
            github_app_id=github_app_id,
            github_app_private_key=github_app_private_key,
            agent_github_app=agent_github_app,
            requester_is_headless=True,
            billing_exempt=billing_exempt,
            memory_read_only=origin == "routine",
            # Only a routine whose result reaches nobody but its owner
            # (`rule_views.routine_answers_only_owner`); read-only like the
            # shared mount, since a routine's memory is always read-only.
            personal_memory=personal_memory,
            # A fire into a shared channel runs held to it: that channel's own
            # (read-only) memory and the held credential, never agent-wide memory.
            hold=hold,
            memory_hold=hold,
            tool_safety=tool_safety,
            budget_channel_id=budget_channel_id,
            origin_channel_id=origin_place.channel_id if origin_place is not None else None,
            origin_thread_id=origin_place.thread_id if origin_place is not None else None,
            origin_seal_ids=origin_place.seal_ids if origin_place is not None else (),
            private_dm_id=origin_place.private_dm_id if origin_place is not None else None,
            model_override=model_override,
            system_override=system_for_platform(agent.system, platform),
        )

    try:
        session = await asyncio.wait_for(
            _assemble_session(),
            timeout=remaining_s(effective_deadline, now=datetime.now(UTC)),
        )
    except TimeoutError as err:
        log.error(
            "turn.ceiling_exceeded",
            phase="headless_session_assembly",
            agent_id=agent_id,
            environment_id=environment_id,
            deadline=effective_deadline.isoformat(),
        )
        raise ceiling_error() from err

    try:
        if (observation := current_outcome.get()) is not None:
            observation.session_id = session.id
            observation.model_by_session[session.id] = model_choice_id(session.agent.model)

        usage_record: Callable[..., Awaitable[object]] | None = None
        if usage_record_factory is not None:
            usage_record = usage_record_factory(session.id, model_choice_id(session.agent.model))

        billing: BillingPosture
        if usage_record is not None:
            _bound_usage_record = usage_record

            async def _record(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
                await _bound_usage_record(event=event)

            billing = Billed(record=_record)
        else:
            billing = BillingExempt(reason="headless-unrecorded")

        cancel = asyncio.Event()  # Headless has no external cancel source.
        persistence: TurnPersistence | None = None
        if selected_path == "mux":
            assert turn_scope is not None and account_id is not None and tenant_id is not None
            # The newly assembled session owns a private synthetic slot. Its
            # claims live for this fire only; no new transcript copy is stored.
            store = MemoryStateStore()
            slot = Slot(
                thread=ThreadRef(
                    channel=ChannelRef(
                        tenant_id=str(tenant_id), platform="headless", channel_id=session.id
                    ),
                    thread_id=session.id,
                ),
                account_id=str(account_id),
            )
            binding = ProviderBinding(
                id=str(uuid.uuid4()),
                thread=slot.thread,
                provider=MANAGED_AGENTS.provider,
                profile=MANAGED_AGENTS.profile_id,
                native_refs={"session": session.id, "agent": agent_id},
                generation=1,
                config_revision=0,
                legacy_account_id=str(account_id),
            )
            try:
                binding = await asyncio.wait_for(
                    store.put_binding(binding, expected_generation=0),
                    timeout=remaining_s(effective_deadline, now=datetime.now(UTC)),
                )
            except TimeoutError as err:
                raise ceiling_error() from err
            persistence = TurnPersistence(
                store,
                binding,
                turn_scope,
                operation_key=f"turn:{session.id}:{uuid.uuid4()}",
                source_agent_id=agent_id,
                retry=MutationRetry(
                    policy=MutationRetryPolicy(max_attempts=anthropic.max_retries + 1),
                    deadline=time.monotonic()
                    + remaining_s(effective_deadline, now=datetime.now(UTC)),
                    cancel=cancel,
                ),
            )

        # Driver's own run_turn builds and sends the `user.message` event itself
        # from `user_message=trigger_message` — the drain (steps 3-6 of the old
        # bespoke loop) is fully delegated below.
        with persistence.activate() if persistence is not None else contextlib.nullcontext():
            state: TurnState = await drive_turn(
                anthropic=anthropic,
                path=selected_path,
                scope=turn_scope,
                session_id=session.id,
                user_message=context_prompt(origin, system=session.agent.system) + trigger_message,
                lifecycle=_NoOpLifecycle(),
                cancel=cancel,
                render_interval_s=2.0,  # nothing renders; do not spin the diff timer
                billing=billing,
                tool_confirmation=headless_tool_confirmation(
                    tool_safety,
                    rules_for=(
                        agent_rule_lookup(
                            session_factory,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            server_urls={s.name: s.url for s in session.agent.mcp_servers},
                            also_urls=daimon_endpoint(
                                str(mcp_settings.public_url)
                                if mcp_settings is not None and mcp_settings.public_url is not None
                                else None
                            ),
                            channel_id=origin_place.channel_id
                            if origin_place is not None
                            else None,
                        )
                        if tool_safety.enabled
                        and tool_safety.agent_rules
                        and session_factory is not None
                        and tenant_id is not None
                        else None
                    ),
                    daimon_rules=bool(daimon_rule_tools),
                    trusted_servers=trusted_servers_for(
                        str(mcp_settings.public_url)
                        if mcp_settings is not None and mcp_settings.public_url is not None
                        else None
                    ),
                ),
                deadline=effective_deadline,
            )

        if (observation := current_outcome.get()) is not None:
            observation.finish(state=state)
        if state.error is not None:
            raise state.error

        if on_state is not None:
            on_state(state)
        return extract_final_response(state.content)
    finally:
        if session_factory is not None:
            try:
                await close_headless_app_session(
                    anthropic, session_factory, session_id=session.id, fernet=fernet
                )
            except Exception:
                log.exception("headless.github_app_session_close.failed", session_id=session.id)


async def commit_worker_transition(
    request: ProviderPreparationRequest,
    parent: WorkerPermissions,
    worker: FleetWorker,
    action: Callable[[AsyncSession, FleetWorker], Awaitable[FleetWorker]] | None = None,
    *,
    failure_code: WorkerFailureCode = "preparation_refused",
    lock_timeout: str | None = None,
) -> FleetWorker:
    async with request.deps.sessionmaker.begin() as db:
        if lock_timeout is not None:
            await db.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
        graph, _ = await fleet_store.lock_budget(
            db,
            tenant_id=request.tenant_id,
            actor_id=parent.fleet.account_id,
            run_id=worker.run_id,
        )
        latest = {row.id: row for _, rows in graph for row in rows}.get(worker.id)
        if latest is None or latest.run_id != worker.run_id:
            raise FleetConflict("worker transition claim missing", reason="claim_missing")
        if action is None:
            if latest.status in TERMINAL:
                updated = latest  # Preserve a concurrent completion/sweep's terminal result.
            else:
                dispatch_changed = (
                    latest.status not in ("starting", "running")
                    or latest.attempts != worker.attempts
                    or latest.version < worker.version
                )
                if failure_code == "preparation_refused" and (
                    (worker.status, worker.binding_id, worker.turns_used) != ("starting", None, 0)
                    or latest.error_code not in (None, "stop_requested")
                    or replace(latest, version=worker.version, error_code=worker.error_code)
                    != worker
                ):
                    # The claim changed but is still this attempt's: end it under the
                    # same-attempt check rather than leave it running.
                    failure_code = "dispatch_refused"
                if dispatch_changed:
                    raise FleetConflict("worker preparation claim changed", reason="claim_changed")
                # A person's Stop is already recorded: the worker ends as stopped, never
                # as a refusal or our failure.
                stopped = latest.error_code == "stop_requested"
                updated = await fleet_store.finish(
                    db,
                    tenant_id=worker.tenant_id,
                    worker_id=worker.id,
                    expected_version=latest.version,
                    status="cancelled" if stopped else "failed",
                    error_code="stop_requested" if stopped else failure_code,
                )
        else:
            updated = await action(db, latest)
        if updated.status in TERMINAL:
            await enqueue_worker_finished(
                db,
                tenant_id=request.tenant_id,
                run_id=worker.run_id,
                now=await database_clock(db),
            )
        return updated


#: How long one attempt to record a worker's failure waits for the tenant's fleet lock,
#: and how many attempts it makes before leaving the row to the deadline sweep.
WORKER_FAILURE_COMMIT_LOCK_TIMEOUT = "5s"
WORKER_FAILURE_COMMIT_ATTEMPTS = 3
#: The most one such attempt may take, statements included.
WORKER_FAILURE_COMMIT_ATTEMPT_S = 8.0
#: Set on an error whose worker failure `run_worker` already recorded (or deferred).
FAILURE_RECORDED = "_daimon_worker_failure_recorded"


async def commit_worker_failure(
    request: ProviderPreparationRequest,
    parent: WorkerPermissions,
    worker: FleetWorker,
    *,
    failure_code: WorkerFailureCode,
) -> None:
    """Record a failed worker without waiting out a busy fleet lock.

    A lock held elsewhere kept J4's failed worker running 49 s past its deadline. After
    the bounded attempts the row stays `starting` and the deadline sweep ends it.
    """
    for _ in range(WORKER_FAILURE_COMMIT_ATTEMPTS):
        try:
            # Bounded as a whole too: this runs shielded, so a slow statement must end.
            async with asyncio.timeout(WORKER_FAILURE_COMMIT_ATTEMPT_S):
                await commit_worker_transition(
                    request,
                    parent,
                    worker,
                    failure_code=failure_code,
                    lock_timeout=WORKER_FAILURE_COMMIT_LOCK_TIMEOUT,
                )
            return
        except FleetConflict:
            return
        except TimeoutError:
            continue
        except DBAPIError as error:
            if not fleet_store.lock_timed_out(error):
                raise
    log.warning(
        "fleet.worker_failure_commit_deferred",
        run_id=str(worker.run_id),
        worker_id=str(worker.id),
        failure_code=failure_code,
        attempts=WORKER_FAILURE_COMMIT_ATTEMPTS,
    )


#: The longest a fleet worker spends on its own setup (policy, source capture, financial
#: admission, backend resolution) before its native session. A stuck step fails the worker
#: in plain words instead of holding it at "starting" until the run's deadline (SMOKE J4).
WORKER_SETUP_TIMEOUT_S = 45.0
#: How long one worker setup transaction waits for the tenant's fleet lock or funds row.
WORKER_SETUP_LOCK_TIMEOUT = "10s"


@contextlib.asynccontextmanager
async def _worker_stage(
    worker: FleetWorker, stage: str, *, setup_at: float | None = None
) -> AsyncIterator[None]:
    """Log one worker stage's outcome and time; a setup stage also ends at `setup_at`."""
    started = time.monotonic()
    outcome, error_class = "error", None
    scope: asyncio.Timeout | None = None
    try:
        if setup_at is None:
            yield
        else:
            async with asyncio.timeout_at(setup_at) as scope:
                yield
        outcome = "ok"
    except TimeoutError as error:
        error_class = type(error).__name__
        if scope is not None and scope.expired():
            outcome = "setup_timeout"
            raise FleetConflict(
                "worker setup did not finish in time", reason="worker_setup_busy"
            ) from error
        raise
    except BaseException as error:
        error_class = type(error).__name__
        raise
    finally:
        log.info(
            "fleet.worker_stage",
            run_id=str(worker.run_id),
            worker_id=str(worker.id),
            stage=stage,
            outcome=outcome,
            error_class=error_class,
            ms=round((time.monotonic() - started) * 1000),
        )


@contextlib.asynccontextmanager
async def _bounded_locks(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """The same short lock wait inside a caller's transaction (the send claim's input check).

    The caller's own lock wait is restored afterwards, so the rest of its transaction
    keeps the setting it had.
    """
    prior = await db.scalar(text("SELECT current_setting('lock_timeout')"))
    await db.execute(
        text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": WORKER_SETUP_LOCK_TIMEOUT},
    )
    try:
        yield db
    except DBAPIError as error:
        if not fleet_store.lock_timed_out(error):
            raise
        raise FleetConflict(
            "the tenant's fleet lock stayed busy before the worker's input",
            reason="worker_setup_busy",
        ) from error
    await db.execute(text("SELECT set_config('lock_timeout', :value, true)"), {"value": prior})


@contextlib.asynccontextmanager
async def _setup_transaction(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A worker setup transaction that gives up on a busy fleet lock instead of waiting."""
    try:
        async with sessionmaker.begin() as db:
            await db.execute(text(f"SET LOCAL lock_timeout = '{WORKER_SETUP_LOCK_TIMEOUT}'"))
            yield db
    except DBAPIError as error:
        if not fleet_store.lock_timed_out(error):
            raise
        raise FleetConflict(
            "the tenant's fleet lock stayed busy during worker setup", reason="worker_setup_busy"
        ) from error


async def run_worker(
    request: ProviderPreparationRequest,
    *,
    parent: WorkerPermissions,
    worker: FleetWorker,
    trigger_message: str,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    cancel: asyncio.Event,
) -> FleetWorker:
    """Drain a pool claim through canonical admission, the fenced turn and its result."""
    deps, admission, revision = request.deps, request.admission, request.admission.backend_revision
    if (
        not parent.fleet.enabled
        or parent.fleet.depth != 0
        or deps.turn_path != "mux"
        or not deps.channel_backends
        or not deps.agent_backends
        or admission.grant is None
        or admission.grant.tenant_id != request.tenant_id
        or admission.grant.subject != parent.subject
        or admission.grant.agent != parent.agent
        or request.tenant_id != worker.tenant_id
        or parent.fleet.run_id != worker.run_id
        or parent.fleet.account_id != admission.account_id
        or worker.status != "starting"
        or worker.binding_id is not None
        or worker.turns_used >= worker.max_turns
        or revision is None
        or request.worker is None
        or (revision.backend, revision.profile) != (worker.backend, worker.profile)
        or request.worker.spec.model.id != worker.model
    ):
        raise FleetConflict(
            "worker dispatch requires its actual admitted pool claim",
            reason="admission_claim_invalid",
        )
    resolved_worker = request.worker
    resolved_worker.limits.require_native_dispatch(request.now())
    # A missing optional journal is composed from the host transaction owner,
    # just as ordinary binding does; an injected foreign owner must never be replaced.
    store = (
        deps.state_store if deps.state_store is not None else PostgresStateStore(deps.sessionmaker)
    )
    if not isinstance(store, PostgresStateStore) or not store.owns_sessions(deps.sessionmaker):
        raise FleetConflict(
            "worker input requires its actual PostgreSQL journal owner",
            reason="journal_owner_mismatch",
        )
    deps = replace(deps, state_store=store)
    request = replace(request, deps=deps)
    commit_transition = functools.partial(commit_worker_transition, request, parent, worker)
    claim, native_started = worker, False
    try:
        bound = turn_deadline(
            now=request.now(), deadline=request.deadline, authority_deadline=worker.deadline_at
        )
        async with asyncio.timeout(remaining_s(bound, now=request.now(), refuse_expired=True)):
            setup_at = asyncio.get_running_loop().time() + WORKER_SETUP_TIMEOUT_S
            async with (
                _worker_stage(worker, "policy", setup_at=setup_at),
                deps.sessionmaker() as db,
            ):
                policy = await policy_store.load_access_policy(db, tenant_id=request.tenant_id)
            source_request = replace(request, scope=parent.fleet.scope, source=None, worker=None)
            async with _worker_stage(worker, "source", setup_at=setup_at):
                source = await capture_preparation_source(source_request)
            definition: Agent | AgentSpec = AgentSpec.model_validate(worker.spec)
            overrides = None
            if resolved_worker.source_agent is not None:
                if source.source is None or resolved_worker.source_agent != (
                    source.source.ref,
                    source.source.revision,
                ):
                    raise FleetConflict(
                        "worker clone source no longer matches its admitted lineage",
                        reason="source_lineage_changed",
                    )
                definition = source.source
                # The admitted spec's native controls (extensions) come from the source;
                # an AgentPatch keys them by namespace, so the spec's list form is left out
                # exactly as admission and preparation do (a raw validate fails on them).
                overrides = clone_overrides(AgentSpec.model_validate(worker.spec))
            normalize = functools.partial(
                resolve_worker_spec,
                parent,
                definition,
                overrides=overrides,
                backend=BackendConfig.model_validate(
                    revision.model_dump(include=set(BackendConfig.model_fields))
                ),
                resources=resolved_worker.resources,
                environment=resolved_worker.environment,
                limits=resolved_worker.limits,
            )
            resolved = normalize(
                policy=policy, now=request.now(), captured_source=source, digest_phase="run_worker"
            )
            changed = [
                name
                for name, now_value, admitted in (
                    ("spec_digest", resolved.digest, worker.spec_digest),
                    ("budget_usd", resolved.limits.budget_usd, worker.budget_usd),
                    ("deadline", resolved.limits.deadline, worker.deadline_at),
                    ("max_turns", resolved.limits.max_turns, worker.max_turns),
                )
                if now_value != admitted
            ]
            if changed:
                # Field names only: what differs between spawn and dispatch, never values.
                log.warning(
                    "fleet.worker_intent_changed",
                    run_id=str(worker.run_id),
                    worker_id=str(worker.id),
                    backend=worker.backend,
                    fields=changed,
                )
                raise FleetConflict(
                    "worker intent no longer resolves under current root authority",
                    reason="intent_changed",
                )
            async with (
                _worker_stage(worker, "admission", setup_at=setup_at),
                _setup_transaction(deps.sessionmaker) as db,
            ):
                usage = await admit_fleet_worker(
                    db,
                    parent=parent,
                    policy=policy,
                    resolved=resolved,
                    ordinal=worker.ordinal,
                    role=worker.role,
                    prices=prices,
                    markup=deps.markup,
                    reviewer_of=worker.reviewer_of,
                    result_contract=worker.result_contract,
                )
            if not usage.admitted or usage.worker.id != worker.id:
                raise FleetConflict(
                    "worker financial admission refused", reason="financial_admission_refused"
                )
            run, current = usage.run, usage.worker
            claim = replace(worker, reserved_usd=current.reserved_usd)
            slot = worker_slot(run, current)
            if slot.thread.channel != revision.channel:
                raise FleetConflict(
                    "worker private slot differs from its admitted channel",
                    reason="private_slot_mismatch",
                )
            prepared: PreparedTurn | None = None
            binding: ProviderBinding | None = None
            archived: set[str] = set()
            operation = spawn_operation_key(run.id, worker.ordinal)
            require_admission = functools.partial(
                require_fleet_worker_admission,
                parent=parent,
                worker_id=worker.id,
                prices=prices,
                markup=deps.markup,
            )

            async def verify_source(db: AsyncSession, source: BackendAgent):
                """The current policy, if the source still resolves to the admitted intent."""
                policy = await policy_store.load_access_policy(db, tenant_id=request.tenant_id)
                if (
                    normalize(policy=policy, now=request.now(), captured_source=source).digest
                    != worker.spec_digest
                ):
                    raise FleetConflict(
                        "worker source intent changed since its admission",
                        reason="source_intent_changed",
                    )
                return policy

            async def recapture(
                db: AsyncSession | None = None, *, captured: BackendAgent | None = None
            ):
                source = (
                    captured
                    if captured is not None
                    else await capture_preparation_source(source_request)
                )
                async with (
                    _setup_transaction(deps.sessionmaker) if db is None else _bounded_locks(db)
                ) as db:
                    policy = await verify_source(db, source)
                    return await require_admission(db, policy=policy, binding=binding)

            async def guard(*, captured: BackendAgent | None = None) -> None:
                remaining_s(bound, now=request.now(), refuse_expired=True)
                if cancel.is_set():
                    raise asyncio.CancelledError
                fresh = await recapture(captured=captured)
                if not fresh.admitted:
                    raise FleetConflict(
                        "worker financial admission refused", reason="financial_admission_refused"
                    )
                remaining_s(bound, now=request.now(), refuse_expired=True)
                if cancel.is_set():
                    raise asyncio.CancelledError

            async def origin_check(db: AsyncSession, source: BackendAgent) -> None:
                # Inside the resolve transaction: only the policy and the source intent.
                # Funding (and with it the tenant fleet lock) stays out of this
                # transaction; the pre-check, before_native and the input claim check it.
                remaining_s(bound, now=request.now(), refuse_expired=True)
                if cancel.is_set():
                    raise asyncio.CancelledError
                await verify_source(db, source)

            async def orphan(result: PreparedTurn) -> None:
                if result.ma_session_id in archived:
                    return
                archived.add(result.ma_session_id)
                backend, ref, scope = result.backend, result.session_ref, result._scope  # pyright: ignore[reportPrivateUsage]
                archive = (
                    functools.partial(
                        backend.sessions.archive,
                        scope,
                        ref,
                        key=f"{operation}:orphan:{result.ma_session_id}",
                    )
                    if backend is not None and ref is not None and scope is not None
                    else None
                )
                io = (
                    None
                    if archive is not None
                    else turn_io(
                        deps.anthropic,
                        result.ma_session_id,
                        **_turn_port_kwargs(
                            deps,
                            result.admission,
                            result.ma_session_id,
                            tenant_id=request.tenant_id,
                            provider_backend=result.backend,
                            provider_session=result.session_ref,
                            provider_runtime=result.runtime,
                            provider_scope=result._scope,  # pyright: ignore[reportPrivateUsage]
                        ),
                    )
                )
                await _archive_orphaned_session(
                    deps.anthropic, session_id=result.ma_session_id, io=io, archive=archive
                )

            async def bound_session(result: PreparedTurn) -> None:
                nonlocal current, prepared, binding
                prepared = result
                persistence = await prepared_persistence(
                    deps,
                    result.admission,
                    tenant_id=request.tenant_id,
                    platform=request.platform,
                    thread_id=slot.thread.thread_id,
                    session_id=result.ma_session_id,
                    mapping_id=result.mapping_id,
                    session_ref=result.session_ref,
                    scope=result._scope,  # pyright: ignore[reportPrivateUsage]
                )
                binding = persistence.binding
                current = await commit_transition(
                    lambda db, latest: fleet_store.mark_running(
                        db,
                        tenant_id=request.tenant_id,
                        worker_id=worker.id,
                        expected_version=latest.version,
                        binding_id=persistence.binding.id,
                    )
                )

            async def publish(state: TurnState) -> None:
                nonlocal current
                persistence = current_persistence.get()
                if persistence is None or prepared is None or prepared.session_ref is None:
                    raise FleetConflict(
                        "worker result requires its active owned journal",
                        reason="result_journal_missing",
                    )
                pointer = await persistence.publish_worker_result(
                    run, current, prepared.session_ref, accepted_event_ids=state.seen_event_ids
                )
                current = await commit_transition(
                    lambda db, latest: fleet_store.record_result(
                        db,
                        tenant_id=request.tenant_id,
                        worker_id=worker.id,
                        expected_version=latest.version,
                        result=pointer,
                    )
                )

            async def claim_input(
                db: AsyncSession,
                scope: Scope,
                key: str,
                fence: Lease | None,
            ) -> bool:
                turn_key = (
                    f"turn:{prepared.ma_session_id}:{operation}:" if prepared is not None else None
                )
                primary = (turn_key or f"{operation}:") + "send:"
                if not key.startswith(primary):
                    return not (
                        resolved.limits.native_budget_usd is not None
                        and turn_key is not None
                        and key.startswith(turn_key)
                        and not key[len(turn_key) :].startswith(
                            ("cancel:", "archive:", "recovery-stop:", "recovery-interrupt:")
                        )
                    )
                if key != primary + "0":
                    raise FleetConflict(
                        "worker input retry requires reconciliation",
                        reason="input_reconciliation_required",
                    )
                if scope != resolved.permissions.fleet.scope or fence is None or fence.slot != slot:
                    raise FleetConflict(
                        "worker input claim differs from its owned private slot",
                        reason="input_claim_mismatch",
                    )
                remaining_s(bound, now=request.now(), refuse_expired=True)
                if cancel.is_set():
                    raise asyncio.CancelledError
                # A fresh capture at claim time: it runs before this claim transaction's
                # first statement (its connection is checked out lazily), so no pooled
                # connection is held over the HTTP read.
                verdict = await recapture(db)
                if not verdict.admitted:
                    return False
                await fleet_store.use_turn(
                    db,
                    tenant_id=request.tenant_id,
                    worker_id=worker.id,
                    expected_version=verdict.worker.version,
                    expected_status="starting" if prepared is None else "running",
                )
                return True

            async def reseed() -> str:
                raise FleetConflict(
                    "worker recovery requires reconciliation, never a fresh session",
                    reason="recovery_reconciliation_required",
                )

            with store.checking_claims(claim_input):
                try:
                    async with _worker_stage(worker, "resolve", setup_at=setup_at):
                        # The source is read over HTTP once, before the resolve transaction:
                        # that transaction holds a connection (and the shared policy fence)
                        # from its first statement, so its origin checks re-verify the policy
                        # and this capture's intent on the same session, without another
                        # read, a second pooled session or the fleet lock (SMOKE d75dd687).
                        resolve_source = await capture_preparation_source(source_request)
                        await guard(captured=resolve_source)
                        async with deps.sessionmaker.begin() as db:
                            retained, _, origin = await resolve_turn_backend(
                                db,
                                revision.channel,
                                source,
                                enabled=True,
                                retained_revision=revision.local,
                                worker=resolved,
                                validate_origin=lambda selection, origin, retained: origin_check(
                                    db, resolve_source
                                ),
                            )
                    if retained != revision or origin != "agent":
                        raise FleetConflict(
                            "worker retained revision no longer authorizes preparation",
                            reason="retained_revision_changed",
                        )
                    allocation = TurnPersistence(
                        store,
                        None,
                        resolved.permissions.fleet.scope,
                        slot=slot,
                        operation_key=operation,
                        now=request.now,
                    )
                    native_started = True
                    async with _worker_stage(worker, "bind"):
                        with allocation.activate():
                            prepared = await allocation.run(
                                lambda: bind_session_impl(
                                    deps,
                                    admission,
                                    tenant_id=request.tenant_id,
                                    platform=request.platform,
                                    external_user_id=request.external_user_id,
                                    thread_id=slot.thread.thread_id,
                                    session_account_id=request.session_account_id,
                                    reuse_existing=True,
                                    deadline=bound,
                                    now=request.now,
                                    worker=resolved,
                                    before_native=guard,
                                    initial_input=UserMessage(
                                        content=(
                                            TextPart(
                                                text=_with_prefix(
                                                    context_prompt(
                                                        "worker", system=admission.agent.system
                                                    ),
                                                    trigger_message,
                                                )
                                            ),
                                        )
                                    ),
                                    _on_prepared=bound_session,
                                    _on_orphan=orphan,
                                )
                            )
                    outcome = await run_prepared_turn(
                        deps,
                        prepared,
                        tenant_id=request.tenant_id,
                        platform=request.platform,
                        thread_id=slot.thread.thread_id,
                        external_user_id=request.external_user_id,
                        user_message=trigger_message,
                        lifecycle=_NoOpLifecycle(on_result=publish),
                        cancel=cancel,
                        reseed_user_message=reseed,
                        recovery_lifecycle=lambda _: _NoOpLifecycle(),
                        origin="worker",
                        attended=False,
                        deadline=bound,
                        now=request.now,
                        operation_key=operation,
                        before_send=guard,
                    )
                    if (
                        outcome.termination != TerminationReason.COMPLETED
                        or current.status != "succeeded"
                    ):
                        # Chained from the turn's own error, so the worker's failure
                        # code names it: the ceiling (a dispatched Gemini turn that
                        # never answered), a provider quota, ...
                        raise FleetConflict(
                            "worker did not produce an authoritative successful result",
                            reason="result_not_successful",
                        ) from outcome.state.error
                    return current
                finally:
                    if prepared is not None:
                        await orphan(prepared)
    except BaseException as error:
        if not native_started:
            from daimon.core.fleet_orchestration import cancel_failure_code, worker_failure_code

            # The dispatcher's own deadline (its asyncio.timeout) shows here as a cancel:
            # that's this worker running out of time, not the fleet ending.
            deadline_hit = isinstance(error, asyncio.CancelledError) and request.now() >= min(
                request.deadline, worker.deadline_at
            )
            # A bug before native start is ours, never "couldn't be set up on that model".
            failure_code = (
                "preparation_deadline_exceeded"
                if deadline_hit
                # The budget watch's own reason, when it is what cancelled this worker.
                else cancel_failure_code(cancel)
                or worker_failure_code(error, "preparation_refused")
            )
            await asyncio.shield(
                commit_worker_failure(request, parent, claim, failure_code=failure_code)
            )
            setattr(error, FAILURE_RECORDED, True)
        if isinstance(error, TimeoutError):
            ceiling = ceiling_error()
            if not native_started:
                setattr(ceiling, FAILURE_RECORDED, True)
            raise ceiling from error
        raise

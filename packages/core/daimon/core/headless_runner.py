"""Non-interactive turn execution.

Open a fresh MA session, then delegate the drain to the core turn driver
(`daimon.core.turn.driver.run_turn`), returning the truncated final-message
tail. A routine turn inherits the driver's full liveness story — status-
checked eventless-cycle reconnect, the per-call read timeout, the cancel
race, the hardened render error policy — instead of a bespoke bare drain
loop, and still auto-approves tool confirmations via `AutoApprove()`.

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
`daimon.core.usage_sweep.sweep_headless_usage` requires to bill a session.
`run_turn` keeps its string `agent_id`/`environment_id` signature (the
scheduler only has ids) and bridges to `create_session`'s SDK-object
signature via `beta.agents.retrieve` / `beta.environments.retrieve`
(mirrors `daimon.adapters.cli.sessions_bootstrap`).

Per `guideline:architecture` "Error propagation" the runner does not
swallow exceptions: `httpx.HTTPError`, `anthropic.APIError`, and a failed
turn's `TurnState.error` (raised as-is, a `TurnError`) all propagate to the
caller (the scheduler's `_fire_one`, which is the boundary).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from cryptography.fernet import MultiFernet
from daimon.core.config import McpSettings
from daimon.core.sessions import create_session
from daimon.core.turn.ceiling import ceiling_error, remaining_s, turn_deadline
from daimon.core.turn.driver import run_turn as drive_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.posture import AutoApprove, Billed, BillingExempt, BillingPosture
from daimon.core.turn.state import TurnState, extract_final_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

LAST_RESULT_TAIL_MAX = 1000
"""Final-message tail is truncated to at most this many characters."""


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

    async def on_render(self, state: TurnState) -> None:
        return None

    async def on_terminal_success(self, state: TurnState) -> None:
        return None

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
    mcp_settings: McpSettings | None = None,
    account_id: uuid.UUID | None = None,
    usage_record_factory: Callable[[str, str], Callable[..., Awaitable[None]]] | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_uuid: uuid.UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    fernet: MultiFernet | None = None,
    github_fallback_pat: str | None = None,
    github_app_id: str | None = None,
    github_app_private_key: str | None = None,
    deadline: datetime | None = None,
) -> str:
    """Run a single non-interactive turn end-to-end and return its tail.

    Flow:

    1. ``beta.agents.retrieve(agent_id)`` / ``beta.environments.retrieve(environment_id)``
       resolve the SDK objects ``create_session`` needs (it takes objects, not
       ids — the scheduler only has ids after `resolve_agent`/`resolve_environment`).
    2. ``create_session(...)`` (from ``daimon.core.sessions``) opens a fresh MA session.
       When ``mcp_settings`` carries both ``public_url`` and ``jwt_secret``,
       ``ensure_agent_mcp_vault`` runs first and the per-agent vault id is
       attached. Both ``account_id`` and ``agent_uuid`` are required in that
       case; missing either raises ``ValueError`` (no fallback to an
       account-scoped vault). ``create_session`` also stamps the
       session's ``metadata`` with ``daimon_tenant``/``daimon_account`` when
       given, which is what makes the session visible to
       ``usage_sweep.sweep_headless_usage``. The repo binding is
       fetched unconditionally inside ``create_session`` — the operator
       ``github_fallback_pat`` clones ``anon:`` (verified-public) bindings
       even without a per-agent PAT.
    3. The drain is delegated to ``daimon.core.turn.driver.run_turn`` with a
       no-op lifecycle (nothing to render to), ``tool_confirmation=AutoApprove()``
       (a routine still auto-allows tool calls), ``deadline=effective_deadline``
       (the same instant that bounded step 1/2's assembly, so the driver's own
       ``remaining_s`` naturally subtracts the time assembly already spent —
       one window, two disjoint legs, no double enforcement), and a billing
       posture of ``Billed(record=usage_record)`` when a
       ``usage_record_factory`` was given, else
       ``BillingExempt(reason="headless-unrecorded")``. A routine turn now
       inherits the driver's full liveness story — status-checked
       eventless-cycle reconnect, the per-call read timeout, the cancel race,
       the hardened render error policy — instead of a bespoke bare drain
       loop with no reconnect and stream-end-as-success.
    4. After the driver returns: ``extract_final_response(state.content)[:1000]``.

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
    context itself via ``functools.partial``, per D-06). The factory shape
    exists because ``model_id`` is only known after ``create_session``
    returns, but adapter callers want to preset their own routine context
    (platform, user, guild) via ``functools.partial`` before fire time.
    """
    effective_deadline = deadline if deadline is not None else turn_deadline(now=datetime.now(UTC))

    async def _assemble_session() -> BetaManagedAgentsSession:
        agent = await anthropic.beta.agents.retrieve(agent_id)
        environment = await anthropic.beta.environments.retrieve(environment_id)
        return await create_session(
            anthropic,
            agent=agent,
            environment=environment,
            mcp_settings=mcp_settings,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_uuid=agent_uuid,
            session_factory=session_factory,
            fernet=fernet,
            github_fallback_pat=github_fallback_pat,
            github_app_id=github_app_id,
            github_app_private_key=github_app_private_key,
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

    usage_record: Callable[..., Awaitable[None]] | None = None
    if usage_record_factory is not None:
        usage_record = usage_record_factory(session.id, session.agent.model.id)

    billing: BillingPosture
    if usage_record is not None:
        _bound_usage_record = usage_record

        async def _record(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
            await _bound_usage_record(event=event)

        billing = Billed(record=_record)
    else:
        billing = BillingExempt(reason="headless-unrecorded")

    # Driver's own run_turn builds and sends the `user.message` event itself
    # from `user_message=trigger_message` — the drain (steps 3-6 of the old
    # bespoke loop) is fully delegated below.
    state: TurnState = await drive_turn(
        anthropic=anthropic,
        session_id=session.id,
        user_message=trigger_message,
        lifecycle=_NoOpLifecycle(),
        cancel=asyncio.Event(),  # never set — headless has no cancel source
        render_interval_s=2.0,  # nothing renders; do not spin the diff timer
        billing=billing,
        tool_confirmation=AutoApprove(),
        deadline=effective_deadline,
    )

    if state.error is not None:
        raise state.error

    return extract_final_response(state.content)[:LAST_RESULT_TAIL_MAX]

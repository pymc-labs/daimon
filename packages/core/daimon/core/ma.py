"""Thin helpers over the anthropic SDK's Managed Agents beta.

Per refinements §6, this module holds ONLY operations whose logic extends
beyond one SDK call: full-history replay (for SSE reconnect rebuilds),
dedup-filtered live streaming, and interrupt-with-ack-wait. Everything else
(create/list/retrieve/archive on agents, environments, sessions) stays a
direct SDK call in its call site; no delegation layer to maintain.

Design rules:
- Free async functions; no class (no cross-call state to own).
- `AsyncAnthropic` is injected by the caller. No module-level client.
- Errors from the SDK (`anthropic.APIError` and subclasses) propagate
  unchanged. The one exception: `send_interrupt_and_wait` converts its own
  timeout — a purely local condition — into `TurnError(kind="interrupt_timeout")`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass

import structlog
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsDeltaEvent,
    BetaManagedAgentsStartEvent,
)
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
)
from daimon.core.errors import DaimonError, TurnError

log = structlog.get_logger()


@dataclass(frozen=True)
class SessionDeletionReport:
    """Per-account upstream session deletion summary.

    `upstream_error` flags that enumeration/deletion aborted on an
    `anthropic.APIError` AFTER the DB purge committed — counts may
    undercount the sessions actually remaining upstream. Set by
    `purge_account`, not by `delete_sessions_for_account` (which only
    absorbs per-session status errors into `failed`).
    """

    deleted: int = 0
    failed: int = 0
    upstream_error: bool = False


# Local alias for SDK ergonomics — short import for call sites. No
# semantic content; the type IS `BetaManagedAgentsSessionEvent`. If the
# SDK renames the union later, update this line.
SessionEvent = BetaManagedAgentsSessionEvent

# Generous default: replay is a paginated GET walk, not a long-lived stream.
# A stalled paginator would otherwise wedge a reconnecting turn forever.
REPLAY_TIMEOUT_S: float = 60.0


async def replay_events(
    anthropic: AsyncAnthropic,
    *,
    session_id: str,
    timeout_s: float = REPLAY_TIMEOUT_S,
) -> list[SessionEvent]:
    """Return the full ordered event history for `session_id`.

    Walks `client.beta.sessions.events.list(session_id=...)` to completion via
    the SDK's async paginator. Used by the turn driver on SSE reconnect to
    rebuild `TurnState` by re-folding the full log (main design §Session Turn
    Pipeline: "Rebuild state from GET /v1/sessions/{id}/events on reconnect").

    Fail-fast on `anthropic.APIError`: callers at the turn-driver edge convert
    this to `TurnError(kind="upstream")` per refinements §7.

    Bounded by `timeout_s` (default `REPLAY_TIMEOUT_S`, 60s): a stalled
    upstream paginator raises `TurnError(kind="upstream")` instead of hanging
    the reconnecting turn forever, with the `TimeoutError` preserved as
    `__cause__`.
    """

    async def _walk() -> list[SessionEvent]:
        events: list[SessionEvent] = []
        async for event in anthropic.beta.sessions.events.list(session_id=session_id):
            events.append(event)
        return events

    try:
        return await asyncio.wait_for(_walk(), timeout=timeout_s)
    except TimeoutError as err:
        raise TurnError(
            kind="upstream",
            message=f"MA event replay did not complete within {timeout_s}s",
        ) from err


async def stream_events_with_dedup(
    anthropic: AsyncAnthropic,
    *,
    session_id: str,
    seen: set[str],
) -> AsyncGenerator[SessionEvent, None]:
    """Yield live session events whose id is not in `seen`.

    The caller owns `seen` as a running ledger of event ids already folded into
    `TurnState.seen_event_ids`. This helper mutates `seen` in place by adding
    each newly-yielded event id.

    Per `docs/references/sse-streaming.md`:
    - The stream does NOT close on `session.status_idle`. Callers must break
      out of their `async for` on a terminal condition; the helper will loop
      indefinitely otherwise.
    - On reconnect, MA re-delivers events we've already folded. Dedup by id is
      the only correctness mechanism — reducers stay pure and are not consulted
      for "have we seen this before."
    """
    stream = await anthropic.beta.sessions.events.stream(session_id=session_id)
    async for event in stream:
        # SDK 0.117 widened the stream union with token-level framing events
        # (event_start / event_delta) that carry no id and are not foldable
        # session events. Daimon folds complete events only, so skip them —
        # this also narrows `event` to BetaManagedAgentsSessionEvent.
        if isinstance(event, BetaManagedAgentsStartEvent | BetaManagedAgentsDeltaEvent):
            continue
        if event.id in seen:
            continue
        seen.add(event.id)
        yield event


# `session.status_idle` stop_reason variants that represent a real terminal
# stop (session is done) rather than `requires_action` (paused on a tool call).
# Per the SDK: BetaManagedAgentsSessionEndTurn / ...RetriesExhausted vs
# ...RequiresAction. See docs/references/managed-agents.md.
_TERMINAL_STOP_REASONS: frozenset[str] = frozenset({"end_turn", "retries_exhausted"})

# For interrupt acks, `requires_action` is ALSO terminal: when the session is
# paused on a tool approval and the user cancels, the interrupt ack arrives as
# `requires_action` idle (session is idle/paused, not running). Treating it as
# terminal here is correct. Do NOT merge this into `_TERMINAL_STOP_REASONS` —
# that constant lists only the variants `send_interrupt_and_wait` treats as
# terminal. `terminal_stop_reason()` (below) is a separate, broader helper:
# the interactive turn driver treats ANY `session.status_idle` (including
# `requires_action`) as stream-terminal. No approval/resume loop is wired for
# interactive surfaces — the driver finalizes a `requires_action` idle as an
# actionable `TurnError(kind="requires_action")`, not blank success.
# `headless_runner` is the one caller that auto-allows tool confirmations
# instead of stopping.
_INTERRUPT_TERMINAL: frozenset[str] = _TERMINAL_STOP_REASONS | frozenset({"requires_action"})


def terminal_stop_reason(event: SessionEvent) -> str | None:
    """Return the ``stop_reason.type`` string for any ``session.status_idle`` event, else None.

    Callers decide which variants count as terminal. The turn driver treats any
    non-None return as terminal; ``send_interrupt_and_wait`` only treats the
    variants listed in ``_TERMINAL_STOP_REASONS`` as terminal.
    """
    if isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
        return event.stop_reason.type
    return None


async def send_interrupt_and_wait(
    anthropic: AsyncAnthropic,
    *,
    session_id: str,
    timeout_s: float = 120.0,
) -> None:
    """Fire `user.interrupt` against `session_id` and block until MA reaches a
    terminal idle or `timeout_s` elapses.

    Per refinements §5 (interrupt UX):
    - POST `user.interrupt`.
    - Wait up to `timeout_s` (default 120s) for a `session.status_idle` whose
      `stop_reason.type` is in `_INTERRUPT_TERMINAL` (`end_turn`,
      `retries_exhausted`, or `requires_action`). `requires_action` IS treated
      as terminal here — it means the session is idle/paused on a tool approval,
      so the interrupt ack is valid and the cancel is complete.
    - On timeout, raise `TurnError(kind="interrupt_timeout")`. The caller (turn
      driver) converts the in-flight turn to a surfaced failure and tears down.

    The caller owns rendering (`lifecycle.on_render("… interrupting")`). This
    helper is pure I/O-and-wait.
    """
    await anthropic.beta.sessions.events.send(
        session_id,
        events=[{"type": "user.interrupt"}],
    )

    async def _wait_for_terminal_idle() -> None:
        stream = await anthropic.beta.sessions.events.stream(session_id=session_id)
        async for event in stream:
            if not isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
                continue
            if event.stop_reason.type in _INTERRUPT_TERMINAL:
                return
        raise TurnError(
            kind="interrupt_timeout",
            message=f"MA SSE stream closed without terminal idle (timeout {timeout_s}s)",
        )

    try:
        await asyncio.wait_for(_wait_for_terminal_idle(), timeout=timeout_s)
    except TimeoutError as err:
        raise TurnError(
            kind="interrupt_timeout",
            message=f"MA did not acknowledge interrupt within {timeout_s}s",
        ) from err


# HTTP status codes that indicate a stale-version conflict on agents.update.
# Characterized by concurrent-update probing:
# MA raises anthropic.ConflictError (status 409) with type "invalid_request_error"
# and message "Concurrent modification detected. Please fetch the latest version and retry."
_VERSION_CONFLICT_STATUSES: frozenset[int] = frozenset({409})


async def update_agent_with_version_retry(
    anthropic: AsyncAnthropic,
    agent_id: str,
    apply_update: Callable[[BetaManagedAgentsAgent], Awaitable[BetaManagedAgentsAgent]],
) -> BetaManagedAgentsAgent:
    """Retrieve `agent_id`, apply `apply_update`, retry once on version conflict.

    Caller contract for `apply_update`:
    - The closure receives the freshly-retrieved `BetaManagedAgentsAgent` and must
      derive `version=` AND any state-derived fields (skill/tool/server unions) FROM
      that argument — never from an earlier read. Stale-read union merges recomputed
      against the fresh agent are the primary use-case; see the four call sites in
      wave-2 plans (72-06, 72-07).
    - Retry is once only: a second conflict propagates unchanged so callers at the
      adapter boundary can map it to an appropriate user-facing error.
    - Non-conflict `APIStatusError` (e.g. 400 validation, 404 not found) propagates
      immediately without a retry attempt.

    Retry-once lives here at the I/O shell; pure logic (reducers, decision functions)
    must not retry internally per guideline:architecture.
    """
    agent = await anthropic.beta.agents.retrieve(agent_id)
    try:
        return await apply_update(agent)
    except APIStatusError as err:
        if err.status_code not in _VERSION_CONFLICT_STATUSES:
            raise
        log.info(
            "ma.update_version_conflict_retry",
            agent_id=agent_id,
            status_code=err.status_code,
        )
        fresh = await anthropic.beta.agents.retrieve(agent_id)
        return await apply_update(fresh)


async def delete_skill_and_versions(anthropic: AsyncAnthropic, skill_id: str) -> None:
    """Delete all versions of a skill on MA, then delete the skill itself.

    Tolerates 404 on individual version deletes: a previous partial cleanup
    attempt may have already deleted some versions.
    """
    async for v in anthropic.beta.skills.versions.list(skill_id, limit=100):
        try:
            await anthropic.beta.skills.versions.delete(v.version, skill_id=skill_id)
        except APIStatusError as err:
            if err.status_code == 404:
                log.info(
                    "skill version already deleted",
                    skill_id=skill_id,
                    version=v.version,
                )
                continue
            raise
    await anthropic.beta.skills.delete(skill_id)


async def delete_entire_workspace_for_testing(
    client: AsyncAnthropic, *, i_understand_this_destroys_all_tenants: bool = False
) -> None:
    """Delete every skill/environment/agent in the shared MA workspace.

    DESTRUCTIVE — the workspace is shared by ALL tenants on the operator's one
    API key. Test-only: the required flag must be set True by the caller; a
    production path that forgets it raises RuntimeError before any MA call.

    Deletion order is dependency-safe: skills (versions first via
    delete_skill_and_versions) → environments (delete, 409 fallback to archive)
    → agents (archive only — DELETE /v1/agents/{id} returns 404).

    Best-effort: continues through all three resource types even if one fails.
    Collects non-404 errors and raises an aggregate DaimonError at the end.
    Tolerates 404 on individual resources.
    """
    if not i_understand_this_destroys_all_tenants:
        raise RuntimeError(
            "delete_entire_workspace_for_testing nukes the shared MA workspace "
            "for ALL tenants; pass i_understand_this_destroys_all_tenants=True "
            "from a test."
        )
    errors: list[Exception] = []

    # Skills: versions first (MA requires this), then skill.
    # 400 = built-in workspace skill with non-UUID id (xlsx, pdf, etc.) — skip.
    # list_skills_lenient: test-only best-effort cleanup; degrade mode is safe here.
    from daimon.core.defaults.ma_index import list_skills_lenient  # noqa: PLC0415

    skills, _truncated = await list_skills_lenient(client)
    for skill in skills:
        try:
            await delete_skill_and_versions(client, skill.id)
        except APIStatusError as err:
            if err.status_code not in (400, 404):
                errors.append(err)
        except Exception as err:
            errors.append(err)

    # Environments: delete; 409 means active sessions → archive fallback
    async for env in client.beta.environments.list(limit=100):
        try:
            await client.beta.environments.delete(env.id)
        except APIStatusError as err:
            if err.status_code == 409:
                try:
                    await client.beta.environments.archive(env.id)
                except APIStatusError as arch_err:
                    if arch_err.status_code != 404:
                        errors.append(arch_err)
            elif err.status_code != 404:
                errors.append(err)

    # Agents: archive only — DELETE /v1/agents/{id} returns 404
    async for agent in client.beta.agents.list(limit=100):
        try:
            await client.beta.agents.archive(agent.id)
        except APIStatusError as err:
            if err.status_code != 404:
                errors.append(err)

    if errors:
        raise DaimonError(
            f"delete_entire_workspace_for_testing: {len(errors)} error(s) during cleanup: "
            + "; ".join(str(e) for e in errors)
        )


async def delete_sessions_for_account(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> SessionDeletionReport:
    """Hard-delete every MA session tagged for `account_id` under `tenant_id`.

    Enumeration: list the tenant's agents (list_agents_by_tenant), then
    sessions.list(agent_id=...) per agent, client-side filter on
    metadata[MA_METADATA_KEY_ACCOUNT] == str(account_id). Best-effort:
    per-session failures are counted, not raised. 404 = already gone
    (idempotent), counted as deleted.
    """
    # Local imports break the circular dependency:
    # ma.py <-> defaults/__init__ -> apply -> reconcile_skills -> ma.py
    from daimon.core.defaults.ma_index import list_agents_by_tenant  # noqa: PLC0415
    from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT  # noqa: PLC0415

    agents = await list_agents_by_tenant(client, tenant_id=tenant_id)

    target_ids: set[str] = set()
    for agent in agents:
        async for session in client.beta.sessions.list(agent_id=agent.id):
            if session.metadata.get(MA_METADATA_KEY_ACCOUNT) == str(account_id):
                target_ids.add(session.id)

    deleted = 0
    failed = 0
    for session_id in target_ids:
        try:
            await client.beta.sessions.delete(session_id)
            deleted += 1
        except APIStatusError as err:
            if err.status_code == 404:
                deleted += 1
            else:
                failed += 1

    return SessionDeletionReport(deleted=deleted, failed=failed)

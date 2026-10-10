"""Managed Agents host policy over scoped resource ports and the turn edge.

Per refinements §6, this module holds ONLY operations whose logic extends
beyond one provider call: replay, interrupt acknowledgement and best-effort
cleanup. Resource I/O uses ports; the remaining replay and acknowledgement
SDK calls belong to the separately owned turn migration.

Design rules:
- Free async functions; no class (no cross-call state to own).
- `AsyncAnthropic` is injected by the caller. No module-level client.
- Errors from the SDK (`anthropic.APIError` and subclasses) propagate
  unchanged. Two exceptions: `send_interrupt_and_wait` converts its own
  timeout — a purely local condition — into `TurnError(kind="interrupt_timeout")`,
  and `interrupt_orphaned_session` is best-effort by contract: it logs an
  SDK error or its own timeout instead of raising.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from anthropic import APIError, APIStatusError, AsyncAnthropic
from anthropic._models import construct_type_unchecked
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.sessions import (
    BetaManagedAgentsSessionEvent,
    BetaManagedAgentsSessionStatusIdleEvent,
)
from daimon.core.errors import DaimonError, TurnError
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import (
    delete_skill,
    delete_skill_version,
    legacy_call,
    legacy_iter,
    list_skill_versions,
    retrieve_agent,
)
from mux.contracts.ids import Scope
from mux.drivers.anthropic.core_admin import CoreAdmin
from mux.drivers.anthropic.transport import LegacyTurnTransport

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
    scope: Scope | None = None,
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

    try:
        return await asyncio.wait_for(
            LegacyTurnTransport(anthropic, session_id, scope=scope).replay(), timeout=timeout_s
        )
    except TimeoutError as err:
        raise TurnError(
            kind="upstream",
            message=f"MA event replay did not complete within {timeout_s}s",
        ) from err


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
    scope: Scope | None = None,
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
    transport = LegacyTurnTransport(anthropic, session_id, scope=scope)
    await transport.send([{"type": "user.interrupt"}])

    async def _wait_for_terminal_idle() -> None:
        stream = await transport.open_interrupt_stream()
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


# The boot orphan sweeps await each interrupt while turn admission waits on
# them, so the call is bounded here rather than by the client's own timeout
# (the adapters' clients retry MA_MAX_RETRIES times, 600 s per attempt).
ORPHAN_INTERRUPT_TIMEOUT_S: float = 10.0


async def interrupt_orphaned_session(
    anthropic: AsyncAnthropic,
    *,
    session_id: str,
    scope: Scope | None,
    timeout_s: float | None = None,
) -> bool:
    """Stop the MA turn a dead adapter process left running. Best-effort.

    A turn's render loop dies with the process that started it, but MA keeps
    running the turn: it goes on billing, and the next mention in the thread
    reuses the session and sends its `user.message` into a session that is
    still running -- MA answers that with 200 and ignores it (see the driver's
    tool-confirmation send), so the new turn would render the dead turn's
    answer. The boot orphan sweeps call this for every row whose marker they
    actually cleared.

    Sends `user.interrupt` without waiting for the session to go idle, and
    gives up after `timeout_s` (default `ORPHAN_INTERRUPT_TIMEOUT_S`, retries
    included): the sweep holds turn
    admission while it runs, so an MA brownout must cost each orphan at most
    `timeout_s`, not the client's retry budget. A session that is already
    idle, archived or gone answers with an error (or a no-op); either way
    there is nothing left to stop. An `anthropic.APIError` or the timeout is
    logged and reported as `False`, never raised.

    ponytail: the sweeps assume one adapter process per platform (see
    `list_orphaned_turns`). A second, overlapping process would see the first
    one's live markers as orphans, and this call would then stop those live
    MA turns, not just relabel their cards.
    """
    if scope is None:
        log.info(
            "turn.orphan_interrupt_skipped", session_id=session_id, reason="missing_account_id"
        )
        return False
    if timeout_s is None:
        timeout_s = ORPHAN_INTERRUPT_TIMEOUT_S
    backend = managed_agents(anthropic, scope=scope, resources=frozenset({("session", session_id)}))
    port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)
    try:
        await asyncio.wait_for(
            legacy_call(
                port.interrupt_orphan(
                    scope, resource_ref(backend, "session", session_id, scope=scope)
                )
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        log.warning("turn.orphan_interrupt_timeout", session_id=session_id, timeout_s=timeout_s)
        return False
    except APIError as err:
        log.info(
            "turn.orphan_interrupt_failed",
            session_id=session_id,
            err_type=type(err).__name__,
            error=str(err)[:200],
        )
        return False
    log.info("turn.orphan_interrupted", session_id=session_id)
    return True


async def update_agent_with_version_retry(
    anthropic: AsyncAnthropic,
    agent_id: str,
    apply_update: Callable[[BetaManagedAgentsAgent], Awaitable[BetaManagedAgentsAgent]],
    *,
    scope: Scope | None = None,
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
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.ma:update_agent_with_version_retry"
    )
    agent = await retrieve_agent(anthropic, agent_id, scope=scope)
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
        fresh = await retrieve_agent(anthropic, agent_id, scope=scope)
        return await apply_update(fresh)


async def delete_skill_and_versions(
    anthropic: AsyncAnthropic, skill_id: str, *, scope: Scope | None = None
) -> None:
    """Delete all versions of a skill on MA, then delete the skill itself.

    Tolerates 404 on individual version deletes: a previous partial cleanup
    attempt may have already deleted some versions.
    """
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.ma:delete_skill_and_versions"
    )
    async for v in list_skill_versions(anthropic, skill_id, limit=100, scope=scope):
        try:
            await delete_skill_version(anthropic, skill_id, v.version, scope=scope)
        except APIStatusError as err:
            if err.status_code == 404:
                log.info(
                    "skill version already deleted",
                    skill_id=skill_id,
                    version=v.version,
                )
                continue
            raise
    await delete_skill(anthropic, skill_id, scope=scope)


# Name of the agent that marks a workspace as disposable. Only the metadata
# decides — the name is a courtesy to whoever finds the agent in the console.
WORKSPACE_SENTINEL_AGENT_NAME = "workspace-disposable-sentinel"

WORKSPACE_NOT_DISPOSABLE_MESSAGE = (
    "Refusing to empty this Managed Agents workspace: it is not marked disposable, "
    "so the destructive cleanup stopped before deleting anything. The marker exists "
    "so that a misrouted API key cannot wipe a shared workspace other installs depend "
    "on. If this workspace really is a throwaway one, mark it with: "
    "uv run python -m daimon.testing.mark_disposable --yes"
)


async def find_workspace_disposable_sentinel(
    client: AsyncAnthropic,
) -> BetaManagedAgentsAgent | None:
    """Return the agent marking this MA workspace as disposable, else None.

    The sentinel is workspace-wide, not tenant-scoped: it answers "may the test
    suite destroy everything reachable from this API key?" and nothing else.
    """
    from daimon.core.defaults.metadata import (
        MA_METADATA_KEY_WORKSPACE,
        MA_METADATA_VALUE_WORKSPACE_DISPOSABLE,
    )

    scope = Scope.platform(
        reason="sentinel cleanup: explicit destructive opt-in, enumerates all tenants"
    )
    backend = managed_agents(client, scope=scope)
    port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)
    async for record in legacy_iter(port.workspace_agents(scope)):
        agent = construct_type_unchecked(value=record, type_=BetaManagedAgentsAgent)
        if agent.metadata.get(MA_METADATA_KEY_WORKSPACE) == MA_METADATA_VALUE_WORKSPACE_DISPOSABLE:
            return agent
    return None


async def delete_entire_workspace_for_testing(
    client: AsyncAnthropic, *, i_understand_this_destroys_all_tenants: bool = False
) -> None:
    """Delete every skill/environment/agent in the shared MA workspace.

    DESTRUCTIVE — the workspace is shared by ALL tenants on the operator's one
    API key. Test-only, and fail-closed twice over: the caller must set the
    required flag (a production path that forgets it raises RuntimeError before
    any MA call), AND the workspace itself must carry a disposable sentinel
    agent (see `find_workspace_disposable_sentinel`). The flag alone is worth
    little — the caller that passes it is the same caller that would be pointed
    at the wrong workspace. The sentinel travels with the workspace instead.

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
    sentinel = await find_workspace_disposable_sentinel(client)
    if sentinel is None:
        raise DaimonError(WORKSPACE_NOT_DISPOSABLE_MESSAGE)
    scope = Scope.platform(
        reason="sentinel cleanup: explicit destructive opt-in, enumerates all tenants"
    )
    backend = managed_agents(client, scope=scope)
    port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)
    errors: list[Exception] = []

    # Skills: versions first (MA requires this), then skill.
    # 400 = built-in workspace skill with non-UUID id (xlsx, pdf, etc.) — skip.
    # list_skills_lenient: test-only best-effort cleanup; degrade mode is safe here.
    from daimon.core.defaults.ma_index import list_skills_lenient

    skills, _truncated = await list_skills_lenient(client)
    for skill in skills:
        try:
            await delete_skill_and_versions(client, skill.id, scope=scope)
        except APIStatusError as err:
            if err.status_code not in (400, 404):
                errors.append(err)
        except Exception as err:
            errors.append(err)

    # Environments: delete; 409 means active sessions → archive fallback
    async for environment_id in legacy_iter(port.workspace_environment_ids(scope)):
        ref = resource_ref(backend, "environment", environment_id, scope=scope)
        try:
            await legacy_call(backend.environments.delete(scope, ref, key=uuid.uuid4().hex))
        except APIStatusError as err:
            if err.status_code == 409:
                try:
                    await legacy_call(
                        backend.environments.archive(scope, ref, key=uuid.uuid4().hex)
                    )
                except APIStatusError as arch_err:
                    if arch_err.status_code != 404:
                        errors.append(arch_err)
            elif err.status_code != 404:
                errors.append(err)

    # Agents: archive only — DELETE /v1/agents/{id} returns 404.
    # The sentinel is spared: archiving it would un-mark the workspace and make
    # the next module's pre-clean refuse.
    async for record in legacy_iter(port.workspace_agents(scope)):
        agent_id = construct_type_unchecked(value=record, type_=BetaManagedAgentsAgent).id
        if agent_id == sentinel.id:
            continue
        try:
            await legacy_call(
                backend.agents.archive(
                    scope,
                    resource_ref(backend, "agent", agent_id, scope=scope),
                    key=uuid.uuid4().hex,
                )
            )
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
    from daimon.core.defaults.ma_index import list_agents_by_tenant
    from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT

    agents = await list_agents_by_tenant(client, tenant_id=tenant_id)
    scope = resource_scope(
        tenant_id=str(tenant_id), account_id=str(account_id), authorization_id="account-purge"
    )
    backend = managed_agents(
        client, scope=scope, resources=frozenset(("agent", agent.id) for agent in agents)
    )
    port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)

    target_ids: set[str] = set()
    for agent in agents:
        async for record in legacy_iter(
            port.sessions_for_agent(scope, resource_ref(backend, "agent", agent.id, scope=scope))
        ):
            session = construct_type_unchecked(value=record, type_=BetaManagedAgentsSession)
            if session.metadata.get(MA_METADATA_KEY_ACCOUNT) == str(account_id):
                target_ids.add(session.id)

    deleted = 0
    failed = 0
    backend = managed_agents(
        client,
        scope=scope,
        resources=frozenset(("session", session_id) for session_id in target_ids),
    )
    port = backend.extension(CoreAdmin, namespace="anthropic.core_admin", version=1)
    for session_id in target_ids:
        try:
            await legacy_call(
                port.delete_session(
                    scope, resource_ref(backend, "session", session_id, scope=scope)
                )
            )
            deleted += 1
        except APIStatusError as err:
            if err.status_code == 404:
                deleted += 1
            else:
                failed += 1

    return SessionDeletionReport(deleted=deleted, failed=failed)

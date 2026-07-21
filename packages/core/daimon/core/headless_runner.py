"""Non-interactive turn execution.

Open a fresh MA session, send a single trigger message, drain the SSE stream
through the reducers until a terminal `session.status_idle` event, then
return the truncated final-message tail.

Used by `daimon.adapters.scheduler` for routine fires and by any
future caller that needs an "agent runs once, returns text" loop without a
human-facing render lifecycle.

Session assembly is delegated to `create_session` in `daimon.core.sessions`
(the same collapse that unified the MCP `start_turn` path) — this is the
single source of truth for vault/PAT/env-mount/repo-resource assembly AND
the `daimon_tenant`/`daimon_account` metadata stamp that
`daimon.core.usage_sweep.sweep_headless_usage` requires to bill a session.
`run_turn` keeps its string `agent_id`/`environment_id` signature (the
scheduler only has ids) and bridges to `create_session`'s SDK-object
signature via `beta.agents.retrieve` / `beta.environments.retrieve`
(mirrors `daimon.adapters.cli.sessions_bootstrap`).

Reuses `daimon.core.turn.reducers.apply` and
`daimon.core.turn.state.extract_final_response`. Does **not** use
`daimon.core.turn.driver.run_turn` — the driver is interactive (lifecycle
hooks, render loop, tenacity retry); the headless runner is a single
straight-through drain.

Per `guideline:architecture` "Error propagation" the runner does not
swallow exceptions: `httpx.HTTPError`, `anthropic.APIError`, and the
`RuntimeError` raised on a `session.error` event all propagate to the
caller (the scheduler's `_fire_one`, which is the boundary).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsDeltaEvent,
    BetaManagedAgentsStartEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event_params import (
    BetaManagedAgentsUserMessageEventParams,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_tool_confirmation_event_params import (
    BetaManagedAgentsUserToolConfirmationEventParams,
)
from cryptography.fernet import MultiFernet
from daimon.core.config import McpSettings
from daimon.core.sessions import create_session
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState, extract_final_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LAST_RESULT_TAIL_MAX = 1000
"""Final-message tail is truncated to at most this many characters."""


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
    3. ``beta.sessions.events.send(session_id, events=[user.message])`` posts
       the trigger.
    4. ``async for event in await beta.sessions.events.stream(session_id=...)``
       drains the stream. Each event is folded into a ``TurnState``
       via ``apply``. The loop terminates on the first ``session.status_idle``
       whose ``stop_reason.type`` is **not** ``requires_action`` — Pitfall 1
       (SSE stays open after idle) is handled by the explicit ``break``.
    5. ``requires_action`` triggers an auto-allow ``user.tool_confirmation``
       send for each blocked event id we have not yet confirmed. The
       ``confirmed`` set is the dedup that prevents double-acks when MA
       re-emits the same blocked id (Pitfall 5). This auto-allow loop is
       headless-only behavior and is unchanged by the ``create_session``
       collapse.
    6. After break: ``extract_final_response(state.content)[:1000]``.

    Errors:

    - A ``session.error`` event raises ``RuntimeError("session.error: ...")``
      with the SDK error's ``message`` (or repr fallback). The reducer also
      records this on ``state.error``, but raising here lets the scheduler's
      boundary catch it as a hard failure rather than reading state on the
      happy path.
    - ``httpx.HTTPError`` and ``anthropic.APIError`` propagate uncaught.

    ``usage_record_factory``, if provided, is invoked once after the MA
    session opens with ``(session.id, session.agent.model.id)`` and must
    return the bound ``usage_record`` callable. The returned callable is
    awaited for each ``span.model_request_end`` event with kwargs
    ``event=event, session_id=session.id``. The factory shape exists
    because ``model_id`` is only known after ``create_session`` returns,
    but adapter callers want to preset their own routine context (platform,
    user, guild) via ``functools.partial`` before fire time.
    """
    agent = await anthropic.beta.agents.retrieve(agent_id)
    environment = await anthropic.beta.environments.retrieve(environment_id)
    session = await create_session(
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

    usage_record: Callable[..., Awaitable[None]] | None = None
    if usage_record_factory is not None:
        usage_record = usage_record_factory(session.id, session.agent.model.id)

    state = TurnState()
    confirmed: set[str] = set()

    user_message: BetaManagedAgentsUserMessageEventParams = {
        "type": "user.message",
        "content": [{"type": "text", "text": trigger_message}],
    }
    await anthropic.beta.sessions.events.send(session.id, events=[user_message])

    async for event in await anthropic.beta.sessions.events.stream(session_id=session.id):
        # SDK 0.117 widened the stream union with token-level framing events
        # (event_start / event_delta) that are not foldable session events.
        # Skip them — this also narrows `event` to BetaManagedAgentsSessionEvent.
        if isinstance(event, BetaManagedAgentsStartEvent | BetaManagedAgentsDeltaEvent):
            continue

        if usage_record is not None and event.type == "span.model_request_end":
            await usage_record(event=event, session_id=session.id)

        state = apply(state, event)

        if isinstance(event, BetaManagedAgentsSessionErrorEvent):
            message = getattr(event.error, "message", None) or repr(event.error)
            raise RuntimeError(f"session.error: {message}")

        if isinstance(event, BetaManagedAgentsSessionStatusIdleEvent):
            if event.stop_reason.type == "requires_action":
                fresh = [tid for tid in event.stop_reason.event_ids if tid not in confirmed]
                if fresh:
                    confirmed.update(fresh)
                    decisions: list[BetaManagedAgentsUserToolConfirmationEventParams] = [
                        {
                            "type": "user.tool_confirmation",
                            "result": "allow",
                            "tool_use_id": tid,
                        }
                        for tid in fresh
                    ]
                    await anthropic.beta.sessions.events.send(session.id, events=decisions)
                continue
            break

    return extract_final_response(state.content)[:LAST_RESULT_TAIL_MAX]

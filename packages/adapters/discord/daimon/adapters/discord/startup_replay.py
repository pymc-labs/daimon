"""Recover completed provider text without sending or billing another turn."""

import asyncio
from datetime import UTC, datetime

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic._models import construct_type_unchecked
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.core.errors import DaimonError
from daimon.core.ma import replay_events
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.core.mux_compat import legacy_call
from daimon.core.platform_words import scrub_other_platforms
from daimon.core.session_ports_compat import session_scope
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.turn.driver import current_turn_events
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState, extract_final_response
from mux.drivers.anthropic import SessionReads

log = structlog.get_logger()
ORPHAN_RESULT_READ_TIMEOUT_S = 5.0


async def completed_orphan_text(
    anthropic_client: AsyncAnthropic, row: ThreadSessionRow
) -> str | None:
    """Recover only a successful current turn; bound reads before closing its card."""
    try:
        async with asyncio.timeout(ORPHAN_RESULT_READ_TIMEOUT_S):
            scope = session_scope(
                tenant_id=row.tenant_id,
                account_id=row.account_id,
                call_site="discord:completed_orphan_text",
            )
            backend = managed_agents(
                anthropic_client,
                scope=scope,
                resources=frozenset({("session", row.ma_session_id)}),
            )
            reads = backend.extension(SessionReads, namespace="anthropic.session_reads", version=1)
            native = await legacy_call(
                reads.read_native(
                    scope, resource_ref(backend, "session", row.ma_session_id, scope=scope)
                )
            )
            # Match SDK partial replies; recovery consumes only status here.
            session = construct_type_unchecked(value=native, type_=BetaManagedAgentsSession)
            if session.status != "idle":
                return None
            events = await replay_events(
                anthropic_client, session_id=row.ma_session_id, timeout_s=4
            )
            current = current_turn_events(events)
            # A marker committed before the user.message send can coexist with
            # an idle previous turn. Never deliver that old answer as this one.
            user_events = [event for event in current if event.type == "user.message"]
            if not user_events or row.active_turn_started_at is None:
                return None
            processed_at = getattr(user_events[-1], "processed_at", None)
            if isinstance(processed_at, str):
                processed_at = datetime.fromisoformat(processed_at.replace("Z", "+00:00"))
            if (
                not isinstance(processed_at, datetime)
                or processed_at.astimezone(UTC) < row.active_turn_started_at
            ):
                return None
            state = TurnState()
            for event in current:
                state = apply(state, event)
            if (
                state.error is not None
                or state.stop_reason is None
                or state.stop_reason.type != "end_turn"
            ):
                return None
            return scrub_other_platforms(extract_final_response(state.content), "discord") or None
    except (anthropic.APIError, TimeoutError, DaimonError) as err:
        log.warning(
            "turn.orphan_result_read_failed", thread_id=row.thread_id, error_type=type(err).__name__
        )
        return None

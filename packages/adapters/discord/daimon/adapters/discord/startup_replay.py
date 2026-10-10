"""Recover completed provider text without sending or billing another turn."""

import asyncio
from datetime import UTC, datetime

import anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.errors import DaimonError
from daimon.core.ma import replay_events
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.turn.driver import current_turn_events
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState, extract_final_response

log = structlog.get_logger()
ORPHAN_RESULT_READ_TIMEOUT_S = 5.0


async def completed_orphan_text(
    anthropic_client: AsyncAnthropic, row: ThreadSessionRow
) -> str | None:
    """Recover only a successful current turn; bound reads before closing its card."""
    try:
        async with asyncio.timeout(ORPHAN_RESULT_READ_TIMEOUT_S):
            session = await anthropic_client.beta.sessions.retrieve(row.ma_session_id)
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
            return extract_final_response(state.content) or None
    except (anthropic.APIError, TimeoutError, DaimonError) as err:
        log.warning(
            "turn.orphan_result_read_failed", thread_id=row.thread_id, error_type=type(err).__name__
        )
        return None

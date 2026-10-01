"""Title for a new bot thread, generated from its opening message.

The Discord half of ``daimon.core.thread_naming``. Awaited before
``create_thread`` so the thread opens under its title: renaming afterwards
posted a "renamed the thread" system message into every new thread. The
Haiku round trip delays the thread by about a second, which is accepted;
``ThreadNamingSettings.timeout_seconds`` bounds the wait.
Called only after the turn's admission passed, so the call is already
behind the balance and cap gates.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import structlog
from anthropic import APIError, AsyncAnthropic
from daimon.core.pricing import MODEL_PRICING
from daimon.core.thread_naming import THREAD_NAMING_MODEL, suggest_thread_name
from daimon.core.usage_recording import record_thread_naming_usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def generate_thread_name(
    *,
    fallback: str,
    message_text: str,
    message_id: int,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform_user_id: str,
    markup: Decimal,
    max_input_chars: int,
    timeout_seconds: float,
) -> str:
    """Title from ``message_text``; ``fallback`` on failure, timeout or a blank answer.

    Metering runs before the return: tokens were spent whatever came back.
    The usage row is keyed on the opening message id, which Discord reuses
    as the id of the thread created from it, so a replay for the same thread
    cannot bill twice. A metering DB error propagates and fails the turn,
    like any other metering error. A timed-out call is cancelled client-side
    and cannot be metered; the cost is bounded by ``max_input_chars`` and
    the output cap, and the event is logged so it can be watched.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            suggestion = await suggest_thread_name(
                anthropic, message_text=message_text, max_input_chars=max_input_chars
            )
    except TimeoutError:
        log.warning(
            "thread.naming_timed_out", message_id=message_id, timeout_seconds=timeout_seconds
        )
        return fallback
    except APIError as exc:
        log.warning("thread.naming_failed", message_id=message_id, error=str(exc))
        return fallback

    await record_thread_naming_usage(
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        model_id=THREAD_NAMING_MODEL,
        input_tokens=suggestion.usage.input_tokens,
        output_tokens=suggestion.usage.output_tokens,
        cache_read_input_tokens=suggestion.usage.cache_read_input_tokens,
        managed_session_id=f"thread-naming:{message_id}",
        event_id="title",
        markup=markup,
        pricing=MODEL_PRICING.get(THREAD_NAMING_MODEL),
    )

    if suggestion.name is None:
        log.info("thread.naming_skipped", message_id=message_id, reason="blank_answer")
        return fallback
    log.info("thread.named", message_id=message_id)
    return suggestion.name

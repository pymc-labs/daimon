"""Background rename of a bot-created thread from its opening message.

The Discord half of ``daimon.core.thread_naming``. Runs as a fire-and-forget
task after ``create_thread`` so the user sees the thread instantly; the
title catches up a second later. Called only after the turn's admission
passed, so the Haiku call is already behind the balance and cap gates.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog
from anthropic import APIError, AsyncAnthropic
from daimon.core.pricing import MODEL_PRICING
from daimon.core.thread_naming import THREAD_NAMING_MODEL, suggest_thread_name
from daimon.core.usage_recording import record_thread_naming_usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger()


async def auto_name_thread(
    *,
    thread: discord.Thread,
    message_text: str,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform_user_id: str,
    markup: Decimal,
    max_input_chars: int,
) -> None:
    """Title ``thread`` from ``message_text``; the placeholder stays on any failure.

    Metering runs before the rename: tokens were spent whether or not
    Discord accepts the edit. A metering DB error propagates to the
    background-task supervisor, never silently.
    """
    try:
        suggestion = await suggest_thread_name(
            anthropic, message_text=message_text, max_input_chars=max_input_chars
        )
    except APIError as exc:
        log.warning("thread.autoname_failed", thread_id=thread.id, error=str(exc))
        return

    await record_thread_naming_usage(
        sessionmaker=sessionmaker,
        tenant_id=tenant_id,
        platform_user_id=platform_user_id,
        model_id=THREAD_NAMING_MODEL,
        input_tokens=suggestion.usage.input_tokens,
        output_tokens=suggestion.usage.output_tokens,
        cache_read_input_tokens=suggestion.usage.cache_read_input_tokens,
        markup=markup,
        pricing=MODEL_PRICING.get(THREAD_NAMING_MODEL),
    )

    if suggestion.name is None:
        log.info("thread.autoname_skipped", thread_id=thread.id)
        return
    try:
        await thread.edit(name=suggestion.name)
    except discord.HTTPException as exc:
        log.warning("thread.autoname_failed", thread_id=thread.id, error=str(exc))
        return
    log.info("thread.autonamed", thread_id=thread.id)

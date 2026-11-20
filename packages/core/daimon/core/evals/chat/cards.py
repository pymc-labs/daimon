"""Cards a tool posts in an eval item's conversation: the MCP process sends them to the eval
worker over Postgres NOTIFY, where the item's script presses them through the real presses."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import structlog
from daimon.core import host_calls
from daimon.core.evals.chat.events import posted_card
from daimon.core.evals.chat.surface import EvalSurfaceLifecycle
from daimon.core.features.snapshot import deployment_on
from daimon.core.posted_controls import PostedCard
from daimon.core.stores import host_call_cards
from daimon.core.turn.deps import TurnDeps
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

EVAL_CARD_CHANNEL: Final = "daimon_evals"


class EvalCardNotice(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: uuid.UUID
    #: "hcc:<id>": the press's card id and the record's message id.
    card_id: str
    card: PostedCard


async def send_eval_card(session: AsyncSession, notice: EvalCardNotice) -> None:
    """Queue the notice; Postgres delivers it when the transaction commits."""
    await session.execute(select(func.pg_notify(EVAL_CARD_CHANNEL, notice.model_dump_json())))


async def press_eval_card(
    deps: TurnDeps, tenant_id: uuid.UUID, surface: EvalSurfaceLifecycle, notice: EvalCardNotice,
    *, is_admin: bool,
) -> None:  # fmt: skip
    """Show the card, let the script press it, and press the host call as that person did.
    An approved call's wake runs as the item's next turn."""
    pressed = await surface.script.answer(posted_card(notice.card_id, notice.card))
    kind, _, raw = notice.card_id.partition(":")
    if pressed is None or kind != "hcc" or pressed.action not in ("approve", "deny"):
        return
    clicker, record = surface.script.requester, uuid.UUID(raw)
    async with deps.sessionmaker() as session:
        row = await host_call_cards.get_card(session, tenant_id=tenant_id, card_id=record)
    if row is None:
        return

    async def current_admin(user_id: str) -> bool:
        return user_id == clicker and is_admin

    settings = deps.settings
    press = await host_calls.press_host_call(
        sessionmaker=deps.sessionmaker,
        enabled=settings is not None and deployment_on(settings, "host_call_gate", row.platform),
        tenant_id=tenant_id, card_id=record, action=pressed.action, clicker=clicker,
        message_id=notice.card_id, current_admin=current_admin, now=datetime.now(UTC),
        queue_wake=False,
    )  # fmt: skip
    if press is None or not press.approved:
        return
    prefix = host_calls.wake_prefix(
        deps.sessionmaker, deps.mcp, tenant_id=tenant_id, platform=row.platform,
        thread_id=row.thread_id, target_ma_agent_id=row.executing_ma_agent_id,
    )  # fmt: skip
    if prefix:
        surface.wakes.append(prefix)


async def listen_cards(
    sessions: async_sessionmaker[AsyncSession],
    handle: Callable[[EvalCardNotice], Awaitable[None]],
    retry_s: float = 5,
) -> None:
    """Deliver eval card notices to `handle` until cancelled; a lost connection reopens."""
    tasks: set[asyncio.Task[None]] = set()

    async def handled(notice: EvalCardNotice) -> None:
        try:
            await handle(notice)
        # One bad card must not stop the listener.
        except Exception:
            log.exception("eval_card.failed", item_id=str(notice.item_id))

    def on_payload(_conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            notice = EvalCardNotice.model_validate_json(payload)
        except ValidationError:
            return
        task = asyncio.create_task(handled(notice))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    while True:
        try:
            async with sessions() as session:
                conn = await session.connection()
                raw = (await conn.get_raw_connection()).driver_connection
                if raw is not None:
                    lost = asyncio.Event()
                    raw.add_termination_listener(lambda _c, lost=lost: lost.set())  # pyright: ignore[reportUnknownLambdaType]
                    await raw.add_listener(EVAL_CARD_CHANNEL, on_payload)
                    await lost.wait()
        except (OSError, SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError) as err:  # pyright: ignore[reportUnknownMemberType]
            log.warning("eval_cards.connection_lost", err_type=type(err).__name__)
        await asyncio.sleep(retry_s)

"""Scenario: a private-input form's queued turn is dispatched at most once,
and is framed as the SAME agent resuming rather than a handoff.

A credential form that lands a value records a `task_continuations` row with
`reason='private_input_applied'` in the transaction that carries the write
(`credential_modals.py` on Discord, `credential_requests.py` on Slack and Teams) and
dispatches nothing itself. The row is picked up the next time any turn
finishes in its origin thread, through the very same
`dispatch_pending_continuations` wiring a `task_handoff` row uses.

This file is the private-input twin of `test_handoff_dispatch.py` and shares
its shape deliberately: the rows are written directly through the core
stores rather than by driving a real form submission (the forms' own
decision logic is covered by each adapter's credential-modal tests), the
destination is the SAME concrete agent the thread already had so
`session_compat` decides a plain reuse, and `_latest_human_message_at` is
boundary-stubbed on Discord because the platform mocks in this suite are
plain `MagicMock(spec=discord.Thread)` objects with no `.history` behavior.

What is proved here that the handoff scenario cannot prove:

- the dispatched turn carries NO `handoff` block. `private_input_applied`
  re-runs the agent that asked for the value, so framing it as "X handed you
  this task" would describe a transfer that never happened. The assertion
  reads the follow-up's actual outgoing `user.message` off the MA transport,
  not the adapter's intent.
- a save-only row (`requested_work is None` — the value landed but nothing
  was ever promised to resume) is skipped silently: no copy posted, no
  billed turn, settled `skipped`/`skip_save_only`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, patch

import pytest
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation, record_continuation
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AGENT_ID, AGENT_TEXT, build_turn_router
from .drivers.discord_driver import DiscordDriver
from .drivers.slack_driver import SlackDriver
from .drivers.teams_driver import TeamsDriver, unsuperseded_continuations

#: The work the form promised to resume once the value landed. Longer than
#: `MIN_REQUESTED_WORK` and distinctive, so it can be found verbatim in the
#: follow-up turn's outgoing user message.
_REQUESTED_WORK = "finish the revenue chart now that the key is set"

_DISCORD_HISTORY = "daimon.adapters.discord.continuation_dispatch._latest_human_message_at"


@dataclass(frozen=True)
class _Thread:
    """One platform's addressing for the single thread every test here uses."""

    platform: Literal["discord", "slack", "teams"]
    workspace_id: str
    user_id: str
    thread_id: str
    parent_channel_id: str


# Discord's driver derives the parent channel as `thread_id - 1`; the Slack
# ids are free-form because Slack carries channel and thread separately.
_DISCORD = _Thread(
    platform="discord",
    workspace_id="900004001",
    user_id="555000444",
    thread_id="310000",
    parent_channel_id="309999",
)
_SLACK = _Thread(
    platform="slack",
    workspace_id="T_PRIVATE_INPUT_PARITY",
    user_id="U_PRIVATE_INPUT_PARITY",
    thread_id="9300000010.000001",
    parent_channel_id="C_PRIVATE_INPUT_PARITY",
)
_TEAMS = _Thread(
    platform="teams",
    workspace_id=str(uuid.UUID(int=900004001)),
    user_id=str(uuid.UUID(int=555000444)),
    thread_id="19:private-input@thread.tacv2;messageid=1700000000000",
    parent_channel_id="19:private-input@thread.tacv2",
)

_EVERY_PLATFORM = pytest.mark.parametrize(
    "place", [_DISCORD, _SLACK, _TEAMS], ids=lambda place: place.platform
)


async def _seed_funded_tenant(
    db_session: AsyncSession, place: _Thread
) -> tuple[uuid.UUID, uuid.UUID]:
    """A funded install plus the requester's account. Returns (tenant_id, account_id)."""
    tenant = await make_tenant(db_session, platform=place.platform, workspace_id=place.workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform=place.platform, external_id=place.user_id
    )
    await db_session.commit()
    return tenant.id, principal.account_id


async def _queue_private_input_continuation(
    sessionmaker: async_sessionmaker[AsyncSession],
    place: _Thread,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    requested_work: str | None,
) -> uuid.UUID:
    """Write the row a consumed private-input request writes, and return its key.

    Exactly what `build_input_continuation` produces from a spent credential
    request: the same concrete agent already answering in this thread (so
    dispatch is exercised in isolation from replacement), and the person's
    own words — or None for a save-only form that promised nothing.
    """
    idempotency_key = uuid.uuid4()
    async with sessionmaker() as session:
        await record_continuation(
            session,
            tenant_id=tenant_id,
            platform=place.platform,
            parent_channel_id=place.parent_channel_id,
            thread_id=place.thread_id,
            requester_account_id=account_id,
            requester_external_user_id=place.user_id,
            target_ma_agent_id=AGENT_ID,
            target_name="test-agent",
            reason="private_input_applied",
            idempotency_key=idempotency_key,
            requested_work=requested_work,
        )
        await session.commit()
    return idempotency_key


async def _mention(
    place: _Thread,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    router: MARouter,
    tenant_id: uuid.UUID,
    text: str,
) -> list[str]:
    """One mention turn through the platform's real entry point.

    Discord's supersede check reads `thread.history`, which the suite's
    `MagicMock(spec=discord.Thread)` does not implement, so it is stubbed to
    "no newer human message" here; Slack's default `conversations.replies`
    stub already returns an empty page and needs no patch. Teams stamps the
    triggering message itself as the latest (see `unsuperseded_continuations`).
    """
    if place.platform == "slack":
        return await SlackDriver().dispatch_turn(
            sessionmaker=sessionmaker,
            router=router,
            tenant_id=tenant_id,
            workspace_id=place.workspace_id,
            channel_id=place.parent_channel_id,
            user_id=place.user_id,
            text=text,
            thread_ts=place.thread_id,
        )
    driver, unsuperseded = (
        (DiscordDriver(), patch(_DISCORD_HISTORY, new_callable=AsyncMock, return_value=None))
        if place.platform == "discord"
        else (TeamsDriver(), unsuperseded_continuations())
    )
    with unsuperseded:
        return await driver.dispatch_turn(
            sessionmaker=sessionmaker,
            router=router,
            tenant_id=tenant_id,
            workspace_id=place.workspace_id,
            channel_id=place.thread_id,
            user_id=place.user_id,
            text=text,
        )


async def _settled(
    sessionmaker: async_sessionmaker[AsyncSession], idempotency_key: uuid.UUID
) -> TaskContinuationRow:
    async with sessionmaker() as session:
        row = await get_continuation(session, idempotency_key=idempotency_key)
    assert row is not None, "the queued continuation row must still exist after dispatch"
    return row


async def _billed_turn_count(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> int:
    async with sessionmaker() as session:
        return len(await usage_events.list_for_tenant(session, tenant_id=tenant_id))


def _outgoing_user_messages(sent_event_bodies: list[dict[str, Any]]) -> list[str]:
    """Every `user.message` text this run actually put on the MA transport."""
    texts: list[str] = []
    for batch in sent_event_bodies:
        for event in cast(list[dict[str, Any]], batch["events"]):
            if event.get("type") != "user.message":
                continue
            texts.append(
                "".join(
                    str(block.get("text", ""))
                    for block in cast(list[dict[str, Any]], event["content"])
                )
            )
    return texts


async def _run_dispatch_scenario(
    place: _Thread,
    *,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    requested_work: str | None,
    sent_event_bodies: list[dict[str, Any]] | None = None,
) -> tuple[uuid.UUID, list[str]]:
    """Turn 1 establishes the session, the row is queued, turn 2 dispatches it.

    Returns the continuation's idempotency key and everything turn 2's single
    platform call posted. Three turns run against the SAME MA session id, and
    `usage_events` keys on `(managed_session_id, event_id)`, so the router
    mints fresh event ids per stream open — otherwise three billed turns
    would silently collapse into one row.
    """
    tenant_id, account_id = await _seed_funded_tenant(db_session, place)
    router = build_turn_router(
        str(tenant_id), fresh_event_ids=True, sent_event_bodies=sent_event_bodies
    )

    posted_1 = await _mention(
        place,
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        text="hello",
    )
    assert any(AGENT_TEXT in posted for posted in posted_1), (
        "turn 1 must post the agent's reply and leave a live session behind"
    )

    idempotency_key = await _queue_private_input_continuation(
        db_session_factory,
        place,
        tenant_id=tenant_id,
        account_id=account_id,
        requested_work=requested_work,
    )

    posted_2 = await _mention(
        place,
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        text="still there?",
    )
    return idempotency_key, posted_2


@_EVERY_PLATFORM
async def test_private_input_continuation_dispatches_exactly_one_follow_up_turn(
    place: _Thread,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The continuation runs exactly once and settles delivered."""
    idempotency_key, posted_2 = await _run_dispatch_scenario(
        place,
        db_session=db_session,
        db_session_factory=db_session_factory,
        requested_work=_REQUESTED_WORK,
    )

    assert sum(1 for posted in posted_2 if AGENT_TEXT in posted) == 2, (
        "turn 2 and the dispatched private-input continuation must each post the agent's "
        "reply exactly once -- a double dispatch would post it a third time"
    )
    row = await _settled(db_session_factory, idempotency_key)
    assert row.status == "delivered", (
        f"a private-input continuation carrying work must settle delivered, got "
        f"{row.status}/{row.skip_reason}"
    )

    tenant_id = row.tenant_id
    assert await _billed_turn_count(db_session_factory, tenant_id) == 3, (
        "turn 1 + turn 2 + the dispatched continuation -- exactly three billed turns"
    )


@_EVERY_PLATFORM
async def test_private_input_continuation_carries_no_handoff_notice(
    place: _Thread,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The dispatched turn's own outgoing message must not frame a handoff."""
    sent_event_bodies: list[dict[str, Any]] = []
    idempotency_key, _posted = await _run_dispatch_scenario(
        place,
        db_session=db_session,
        db_session_factory=db_session_factory,
        requested_work=_REQUESTED_WORK,
        sent_event_bodies=sent_event_bodies,
    )
    row = await _settled(db_session_factory, idempotency_key)
    assert row.status == "delivered", "precondition: the continuation must have run a turn"

    follow_ups = [
        text for text in _outgoing_user_messages(sent_event_bodies) if _REQUESTED_WORK in text
    ]
    assert len(follow_ups) == 1, (
        f"exactly one outgoing message must carry the requested work, got {len(follow_ups)}"
    )
    follow_up = follow_ups[0]
    assert '"handoff"' not in follow_up, (
        "a private-input continuation re-runs the agent that asked for the value, so its "
        "turn controls must carry no handoff block"
    )
    assert "takes over" not in follow_up, (
        "no handoff copy may reach a turn where nothing was handed over"
    )
    assert "If handoff is present" not in follow_up, (
        "the continuity paragraph is appended only alongside a handoff or session_state "
        "block; a plain same-agent resume gets neither"
    )


@_EVERY_PLATFORM
async def test_save_only_private_input_row_is_skipped_silently(
    place: _Thread,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A form that promised nothing costs nothing and says nothing."""
    idempotency_key, posted_2 = await _run_dispatch_scenario(
        place,
        db_session=db_session,
        db_session_factory=db_session_factory,
        requested_work=None,
    )

    assert sum(1 for posted in posted_2 if AGENT_TEXT in posted) == 1, (
        "a save-only row must not run a second turn -- only turn 2's own reply is posted"
    )
    row = await _settled(db_session_factory, idempotency_key)
    assert row.status == "skipped", (
        f"a save-only continuation must be skipped, not delivered; got {row.status}"
    )
    assert row.skip_reason == "skip_save_only", (
        "the skip reason is the decision's own action, so the audit trail says why no "
        "turn was spent"
    )
    assert row.delivered_at is None, "a skipped row must never read as though a turn ran"

    assert await _billed_turn_count(db_session_factory, row.tenant_id) == 2, (
        "turn 1 + turn 2 only -- a save-only continuation bills nothing"
    )

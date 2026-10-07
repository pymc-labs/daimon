"""Scenario: dead-session recovery driven through the platform entry point.

A turn against a live `thread_sessions` row whose MA session has expired/been
GC'd (confirmed signal: 404 `not_found_error` from `events.send`) must mark
the stale row dead, create a fresh MA session + a new live row, re-run once
with full history, and bill the NEW session id -- never the dead one. This is
`run_prepared_turn`'s one-shot recovery cycle (D-08/D-09/D-10), proven here at
the real platform entry point (`DaimonBot.on_message` / `SlackApp._handle_app_mention`)
rather than only at the core unit-test level
(`packages/core/tests/turn/test_run_prepared_turn.py`).

Both platforms: Discord landed with the 06-06 cutover; the Slack scenario
below was added once Slack's own cutover (06-07) closed its dead-session gap.
The router fixture is platform-agnostic (it only fakes the `/v1/...` MA
endpoints), so both scenarios share `_build_dead_session_router`. Teams runs
the Discord half: both address a turn by its thread id alone.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import pytest
from daimon.core.agent_post_identity import fallback_name_prefix
from daimon.core.continuity.messages import render_unexpected_loss
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
)
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, not_found_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AGENT_ID, build_turn_router
from .drivers.discord_driver import DiscordDriver
from .drivers.protocol import thread_ids
from .drivers.slack_driver import SlackDriver
from .drivers.teams_driver import TeamsDriver

# The Discord driver's `create_session` stub always returns this fixed session
# id for the recreate call (see the `ma_session(...)` stub in
# `DiscordDriver.dispatch_turn`) -- the recovered-turn's NEW session id in
# this scenario.
_RECOVERED_SESSION_ID = "sess_parity_test"
_DEAD_SESSION_ID = "sess_dead_before_recovery"


def _build_dead_session_router(tenant_id_str: str) -> MARouter:
    """The shared turn router with its event routes scoped to the recreated
    session, plus the OLD (already-dead) session: its SSE stream open 404s
    (the confirmed dead-session signal -- `run_turn` opens the stream before
    posting the initial user message, so the 404 surfaces there).
    """
    router = build_turn_router(
        tenant_id_str,
        session_id=_RECOVERED_SESSION_ID,
        usage_event_id="evt_parity_recover_usage",
    )
    router.add(
        "GET",
        rf"/v1/sessions/{_DEAD_SESSION_ID}/events/stream",
        lambda req, _m: not_found_response("session gone"),
    )
    # ... and retrieving it 404s too: the pre-continuity row carries no record
    # of what its session was running, so the bind tries to read it. A gone
    # session leaves that to the recovery cycle below rather than failing.
    router.add(
        "GET",
        rf"/v1/sessions/{_DEAD_SESSION_ID}",
        lambda req, _m: not_found_response("session gone"),
    )
    return router


@pytest.mark.parametrize("platform", ["discord", "teams"])
async def test_dead_session_recreates_marks_old_row_dead_and_bills_new_session(
    platform: Literal["discord", "teams"],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, _parent, thread_id = thread_ids(
        platform, workspace=900002001, user=555000222, thread=200000
    )

    tenant = await make_tenant(db_session, platform=platform, workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    # Identity resolution inside admit() is idempotent get-or-create -- pre-creating
    # here just lets the test learn the account_id up front to seed the live row.
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform=platform, external_id=user_id
    )
    old_row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform=platform,
        thread_id=thread_id,
        account_id=principal.account_id,
        ma_session_id=_DEAD_SESSION_ID,
        ma_agent_id=AGENT_ID,
        watermark_message_id="100",
    )
    await db_session.commit()

    router = _build_dead_session_router(str(tenant.id))
    driver = DiscordDriver() if platform == "discord" else TeamsDriver()
    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=thread_id,
        user_id=user_id,
        text="hello after recovery",
    )
    assert posted, f"expected the recovered turn's reply to be posted, got: {posted}"

    # A dead-session recovery must tell the person their workspace was lost,
    # exactly once, and ABOVE the answer it explains -- the answer is an
    # in-place edit of the embed posted at mention time, so a notice sent as
    # its own message would always sort below it. This scenario's dead-session
    # signal is a 404 (deleted session), never the archived-400 signature the
    # transcript rescue targets, so the history variant is the one that
    # must appear.
    expected_notice = render_unexpected_loss("history")
    carrying = [text for text in posted if expected_notice in text]
    assert len(carrying) == 1, (
        f"expected exactly one unexpected-loss notice (history variant), got: {posted}"
    )
    expected_start = expected_notice + "\n\n"
    if platform == "discord":
        expected_start = fallback_name_prefix("test-agent", expected_start)
    assert carrying[0].startswith(expected_start), (
        f"the loss notice must be the first paragraph of the answer, got: {carrying[0]!r}"
    )
    assert carrying[0] != expected_notice, (
        "the notice must ride the answer, not stand alone as a trailing message"
    )

    # The stale mapping is marked dead.
    dead_row = await get_thread_session_by_id(db_session, id=old_row.id)
    assert dead_row is not None, "the pre-existing mapping row must still exist"
    assert dead_row.status == "dead", "the pre-existing mapping must be marked dead"

    # A new live row exists with a different ma_session_id.
    live_row = await get_live_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform=platform,
        thread_id=thread_id,
        account_id=principal.account_id,
    )
    assert live_row is not None, "a new live row must exist after recovery"
    assert live_row.id != old_row.id, "the new live row must be a distinct row"
    assert live_row.ma_session_id == _RECOVERED_SESSION_ID, (
        "the new live row must store the recreated session id"
    )
    assert live_row.ma_session_id != _DEAD_SESSION_ID, (
        "the new live row must NOT reuse the dead session id"
    )

    # The turn produced a final assistant message (already asserted via `posted`
    # above, since the driver only records message.channel.send / thread.send /
    # message_ref.edit text calls).

    # usage_events must bill the NEW session id, never the dead one (the exact
    # attribution bug D-08/D-09/D-10 fixes -- RESEARCH § "Known attribution bug").
    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(usage_rows) == 1, "the recovered turn must write exactly one usage_events row"
    assert usage_rows[0].managed_session_id == _RECOVERED_SESSION_ID, (
        "usage_events must attribute the recovered turn to the NEW session id"
    )
    assert usage_rows[0].managed_session_id != _DEAD_SESSION_ID, (
        "usage_events must never attribute the recovered turn to the dead session id"
    )

    ledger_rows = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    debit_rows = [row for row in ledger_rows if row.delta_usd < 0]
    assert len(debit_rows) == 1, "the recovered turn must write exactly one tenant_ledger debit"


async def test_dead_session_recreates_marks_old_row_dead_and_bills_new_session_slack(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Slack half of the same scenario -- proves the D-08/D-09/D-10 recovery
    cycle at `SlackApp._handle_app_mention`, the real entry point 06-07 cut
    over onto `run_prepared_turn`. Before 06-07 this loop never terminated:
    the discarded `run_turn` return value meant a dead Slack session recreated
    on every turn but the stale mapping was never superseded by a new live row.
    """
    workspace_id = "T_DEAD_SESSION_PARITY"
    user_id = "U_DEAD_SESSION_PARITY"
    thread_id = "9000005000.000001"

    tenant = await make_tenant(db_session, platform="slack", workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    # Identity resolution inside admit() is idempotent get-or-create -- pre-creating
    # here just lets the test learn the account_id up front to seed the live row.
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id=user_id
    )
    old_row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        thread_id=thread_id,
        account_id=principal.account_id,
        ma_session_id=_DEAD_SESSION_ID,
        ma_agent_id=AGENT_ID,
        watermark_message_id="9000005000.000000",
    )
    await db_session.commit()

    router = _build_dead_session_router(str(tenant.id))
    driver = SlackDriver()
    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id="C_DEAD_SESSION_PARITY",
        user_id=user_id,
        text="hello after recovery",
        thread_ts=thread_id,
    )
    assert posted, f"expected the recovered turn's reply to be posted, got: {posted}"

    # A dead-session recovery must tell the person their workspace was lost,
    # exactly once and above the answer it explains, matching Discord.
    expected_notice = render_unexpected_loss("history")
    carrying = [text for text in posted if expected_notice in text]
    assert len(carrying) == 1, (
        f"expected exactly one unexpected-loss notice (history variant), got: {posted}"
    )
    assert carrying[0].startswith(expected_notice + "\n\n"), (
        f"the loss notice must be the first paragraph of the answer, got: {carrying[0]!r}"
    )
    assert carrying[0] != expected_notice, (
        "the notice must ride the answer, not stand alone as a trailing message"
    )

    # The stale mapping is marked dead.
    dead_row = await get_thread_session_by_id(db_session, id=old_row.id)
    assert dead_row is not None, "the pre-existing mapping row must still exist"
    assert dead_row.status == "dead", "the pre-existing mapping must be marked dead"

    # A new live row exists with a different ma_session_id.
    live_row = await get_live_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        thread_id=thread_id,
        account_id=principal.account_id,
    )
    assert live_row is not None, "a new live row must exist after recovery"
    assert live_row.id != old_row.id, "the new live row must be a distinct row"
    assert live_row.ma_session_id == _RECOVERED_SESSION_ID, (
        "the new live row must store the recreated session id"
    )
    assert live_row.ma_session_id != _DEAD_SESSION_ID, (
        "the new live row must NOT reuse the dead session id"
    )

    # usage_events must bill the NEW session id, never the dead one -- this is
    # the exact assertion that would have caught Slack's permanent dead-session
    # loop (the discarded run_turn return value pre-06-07).
    usage_rows = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    assert len(usage_rows) == 1, "the recovered turn must write exactly one usage_events row"
    assert usage_rows[0].managed_session_id == _RECOVERED_SESSION_ID, (
        "usage_events must attribute the recovered turn to the NEW session id"
    )
    assert usage_rows[0].managed_session_id != _DEAD_SESSION_ID, (
        "usage_events must never attribute the recovered turn to the dead session id"
    )

    ledger_rows = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    debit_rows = [row for row in ledger_rows if row.delta_usd < 0]
    assert len(debit_rows) == 1, "the recovered turn must write exactly one tenant_ledger debit"

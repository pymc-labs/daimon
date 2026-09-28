"""FEAT-085: routine destination, fallback post outbox, and its policy gate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    DeliveryTarget,
    agent_posted_to,
    check_delivery,
    delivery_refusal,
    delivery_target,
    poll_deliveries_once,
    render_fallback_post,
    render_routine_controls,
)
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    claim_routine_deliveries,
    create_routine,
    get_routine,
    record_result,
    settle_routine_delivery,
    update_routine,
)
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _row(**overrides: object) -> RoutineRow:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "created_by_user_id": "U1",
        "agent_id": "ag",
        "agent_name": "daimon",
        "cron_expr": "0 9 * * 1",
        "timezone": "UTC",
        "trigger_message": "go",
        "enabled": True,
        "next_fire_at": None,
        "last_fired_at": None,
        "last_error": None,
        "last_result_tail": "All green.",
        "destination_kind": "channel",
        "destination_id": "C1",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return RoutineRow.model_validate(base)


# --- pure --------------------------------------------------------------------


def test_a_slack_thread_destination_is_channel_and_ts() -> None:
    row = _row(destination_kind="thread", destination_id="C1:1717.5")
    assert delivery_target(row, platform="slack") == DeliveryTarget("C1", "1717.5")
    assert (
        delivery_target(_row(destination_kind="thread", destination_id="C1"), platform="slack")
        is None
    )
    assert delivery_target(row, platform="discord") == DeliveryTarget("C1:1717.5")
    assert (
        delivery_target(_row(destination_kind=None, destination_id=None), platform="slack") is None
    )


def test_routine_controls_name_the_destination_and_schedule() -> None:
    text = render_routine_controls(_row(), platform="discord")
    assert text.startswith("<turn_controls>\n{")
    assert '"channel_id": "C1"' in text and '"schedule": "0 9 * * 1"' in text
    assert "posts the end of your final reply there" in text
    assert text.endswith("</turn_controls>")


def _post_block(
    channel_id: str, *, status: str = "complete", is_error: bool = False
) -> ToolUseBlock:
    return ToolUseBlock(
        kind="tool_use",
        id="tu",
        type="agent.mcp_tool_use",
        name="send_message",
        input={"channel_id": channel_id},
        mcp_server_name="daimon-mcp",
        status=status,  # type: ignore[arg-type]
        is_error=is_error,
    )


def test_agent_posted_only_counts_a_successful_post_to_the_destination() -> None:
    row = _row()
    assert agent_posted_to(TurnState(content=[_post_block("C1")]), row, platform="slack")
    assert not agent_posted_to(TurnState(content=[_post_block("C9")]), row, platform="slack")
    assert not agent_posted_to(
        TurnState(content=[_post_block("C1", is_error=True)]), row, platform="slack"
    )
    assert not agent_posted_to(
        TurnState(content=[_post_block("C1", status="failed")]), row, platform="slack"
    )
    assert not agent_posted_to(TurnState(), row, platform="slack")


def test_the_fallback_post_carries_the_tail() -> None:
    assert render_fallback_post(_row()).endswith("\n\nAll green.")


@pytest.mark.parametrize(
    ("policy", "parent", "category", "creator", "admin", "expected"),
    [
        (TenantAccessPolicy(), None, None, "U1", False, None),
        (
            TenantAccessPolicy(protected_channel_ids=("C1",)),
            None,
            None,
            "U1",
            True,
            "protected_channel",
        ),
        (
            TenantAccessPolicy(protected_channel_ids=("P",)),
            "P",
            None,
            "U1",
            False,
            "protected_channel",
        ),
        (
            TenantAccessPolicy(protected_category_ids=("K",)),
            None,
            "K",
            "U1",
            False,
            "protected_channel",
        ),
        (
            TenantAccessPolicy(invoker_user_ids=("U2",)),
            None,
            None,
            "U1",
            False,
            "invoker_not_allowed",
        ),
        (TenantAccessPolicy(invoker_user_ids=("U2",)), None, None, "U1", True, None),
        (TenantAccessPolicy(), None, None, None, False, "invoker_not_allowed"),
    ],
)
def test_delivery_refusal(
    policy: TenantAccessPolicy,
    parent: str | None,
    category: str | None,
    creator: str | None,
    admin: bool,
    expected: str | None,
) -> None:
    assert (
        delivery_refusal(
            policy,
            target=DeliveryTarget("C1"),
            creator_platform_user_id=creator,
            creator_is_admin=admin,
            parent_channel_id=parent,
            category_id=category,
        )
        == expected
    )


# --- outbox (real Postgres) ----------------------------------------------------


async def _routine(
    session: AsyncSession, *, platform: str = "discord", destination: bool = True
) -> RoutineRow:
    tenant = await make_tenant(session, platform=platform)  # type: ignore[arg-type]
    return await create_routine(
        session,
        tenant_id=tenant.id,
        created_by_user_id="U1",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="go",
        destination_kind="channel" if destination else None,
        destination_id="123" if destination else None,
    )


async def test_record_result_without_delivery_leaves_the_outbox_alone(
    db_session: AsyncSession,
) -> None:
    row = await _routine(db_session, destination=False)
    await record_result(db_session, row.id, tail="t", error=None)
    after = await get_routine(db_session, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.delivery_status is None


async def test_a_pending_post_is_claimed_once_and_settled_by_its_owner(
    db_session: AsyncSession,
) -> None:
    row = await _routine(db_session)
    await record_result(db_session, row.id, tail="t", error=None, delivery="pending")

    first = await claim_routine_deliveries(
        db_session, platform="discord", owner="a", now=_NOW, lease=timedelta(minutes=2)
    )
    second = await claim_routine_deliveries(
        db_session, platform="discord", owner="b", now=_NOW, lease=timedelta(minutes=2)
    )
    assert [r.id for r in first] == [row.id]
    assert second == [], "a claimed post is not handed out twice"
    assert not await settle_routine_delivery(
        db_session, row.id, owner="b", status="delivered", now=_NOW
    ), "only the owner settles"
    assert await settle_routine_delivery(
        db_session, row.id, owner="a", status="delivered", now=_NOW
    )
    after = await get_routine(db_session, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.delivery_status == "delivered"
    assert after.delivered_at == _NOW


async def test_an_expired_claim_is_never_reposted(db_session: AsyncSession) -> None:
    row = await _routine(db_session)
    await record_result(db_session, row.id, tail="t", error=None, delivery="pending")
    await claim_routine_deliveries(
        db_session, platform="discord", owner="a", now=_NOW, lease=timedelta(minutes=2)
    )

    later = _NOW + timedelta(minutes=5)
    again = await claim_routine_deliveries(
        db_session, platform="discord", owner="b", now=later, lease=timedelta(minutes=2)
    )

    assert again == []
    after = await get_routine(db_session, row.id, tenant_id=row.tenant_id)
    assert after is not None
    assert (after.delivery_status, after.delivery_note) == ("skipped", "interrupted")


async def test_claims_are_per_platform(db_session: AsyncSession) -> None:
    row = await _routine(db_session, platform="slack")
    await record_result(db_session, row.id, tail="t", error=None, delivery="pending")
    assert (
        await claim_routine_deliveries(
            db_session, platform="discord", owner="a", now=_NOW, lease=timedelta(minutes=2)
        )
        == []
    )


async def test_clearing_the_destination_drops_a_pending_post(db_session: AsyncSession) -> None:
    row = await _routine(db_session)
    await record_result(db_session, row.id, tail="t", error=None, delivery="pending")
    await update_routine(db_session, row.id, tenant_id=row.tenant_id, clear_destination=True)
    after = await get_routine(db_session, row.id, tenant_id=row.tenant_id)
    assert after is not None
    assert (after.destination_kind, after.destination_id, after.delivery_status) == (
        None,
        None,
        None,
    )


async def test_the_poller_posts_and_settles(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    await record_result(db_session, row.id, tail="All green.", error=None, delivery="pending")
    await db_session.commit()
    posted: list[str] = []

    async def post(claimed: RoutineRow) -> DeliveryOutcome:
        posted.append(render_fallback_post(claimed))
        return DeliveryOutcome(status="delivered")

    count = await poll_deliveries_once(db_session_factory, platform="discord", post=post, now=_NOW)
    again = await poll_deliveries_once(db_session_factory, platform="discord", post=post, now=_NOW)

    assert (count, again) == (1, 0)
    assert len(posted) == 1 and posted[0].endswith("All green.")
    async with db_session_factory() as s:
        after = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.delivery_status == "delivered"


async def test_a_poster_that_raises_settles_skipped_never_retried(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    await record_result(db_session, row.id, tail="t", error=None, delivery="pending")
    await db_session.commit()

    async def post(claimed: RoutineRow) -> DeliveryOutcome:
        raise RuntimeError("chat API down")

    await poll_deliveries_once(db_session_factory, platform="discord", post=post, now=_NOW)

    async with db_session_factory() as s:
        after = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert after is not None
    assert (after.delivery_status, after.delivery_note) == ("skipped", "post_failed")


async def test_an_empty_tail_is_not_posted(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    await record_result(db_session, row.id, tail="  ", error=None, delivery="pending")
    await db_session.commit()

    async def post(claimed: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("an empty result must not be posted")

    await poll_deliveries_once(db_session_factory, platform="discord", post=post, now=_NOW)

    async with db_session_factory() as s:
        after = await get_routine(s, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.delivery_note == "no_result"


async def test_check_delivery_applies_the_stored_policy(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_category_ids=("CAT",)),
    )
    await db_session.commit()

    refused = await check_delivery(
        db_session_factory, row, platform="discord", target=DeliveryTarget("123"), category_id="CAT"
    )
    allowed = await check_delivery(
        db_session_factory, row, platform="discord", target=DeliveryTarget("123")
    )

    assert (refused, allowed) == ("protected_channel", None)

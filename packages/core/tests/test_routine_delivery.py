"""FEAT-085: routine destination, fallback post outbox, and its policy gate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from daimon.core import routine_delivery as routine_delivery_module
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_identity import AgentIdentity
from daimon.core.config import Settings
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    DeliveryTarget,
    agent_posted_to,
    clear_creator,
    delivery_refusal,
    delivery_target,
    destination_shape_error,
    placement_unknown_is_unsafe,
    poll_deliveries_once,
    render_fallback_post,
    render_routine_controls,
    resolve_routine_identity,
    teams_thread_id,
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
        "delivery_payload": "All green.",
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


def test_a_teams_thread_destination_is_its_channel_and_root() -> None:
    thread = "19:abc@thread.tacv2;messageid=17"
    row = _row(destination_kind="thread", destination_id=thread)
    target = delivery_target(row, platform="teams")
    assert target == DeliveryTarget("19:abc@thread.tacv2", "17"), "the channel is policy-checked"
    assert target is not None and teams_thread_id(target) == thread
    controls = render_routine_controls(row, platform="teams")
    assert f'"thread_id": "{thread}"' in controls, "the agent posts to the thread by its id"
    malformed = _row(destination_kind="thread", destination_id="19:abc@thread.tacv2")
    assert delivery_target(malformed, platform="teams") is None


def test_routine_controls_name_the_destination_and_schedule() -> None:
    text = render_routine_controls(_row(), platform="discord")
    assert text.startswith("<turn_controls>\n{")
    assert '"channel_id": "C1"' in text and '"schedule": "0 9 * * 1"' in text
    assert "posts the end of your final reply there" in text
    assert text.endswith("</turn_controls>")


def _post_block(
    channel_id: str,
    *,
    status: str = "complete",
    is_error: bool = False,
    via_call_tool: bool = False,
    server: str = "daimon-mcp",
) -> ToolUseBlock:
    arguments: dict[str, object] = {"channel_id": channel_id, "content": "done"}
    return ToolUseBlock(
        kind="tool_use",
        id="tu",
        type="agent.mcp_tool_use",
        name="call_tool" if via_call_tool else "send_message",
        input={"name": "send_message", "arguments": arguments} if via_call_tool else arguments,
        mcp_server_name=server,
        status=status,  # type: ignore[arg-type]
        is_error=is_error,
    )


def test_agent_posted_only_counts_a_successful_post_to_the_destination() -> None:
    row = _row()
    assert agent_posted_to(TurnState(content=[_post_block("C1")]), row)
    assert not agent_posted_to(TurnState(content=[_post_block("C9")]), row)
    assert not agent_posted_to(TurnState(content=[_post_block("C1", is_error=True)]), row)
    assert not agent_posted_to(TurnState(content=[_post_block("C1", status="failed")]), row)
    assert not agent_posted_to(TurnState(content=[_post_block("C1", server="other")]), row)
    assert not agent_posted_to(TurnState(), row)


def test_a_post_through_call_tool_counts() -> None:
    row = _row()
    assert agent_posted_to(TurnState(content=[_post_block("C1", via_call_tool=True)]), row)
    assert not agent_posted_to(
        TurnState(content=[_post_block("C1", via_call_tool=True, is_error=True)]), row
    )


def test_a_slack_thread_destination_needs_a_post_into_that_thread() -> None:
    row = _row(destination_kind="thread", destination_id="C1:1717.5")
    assert agent_posted_to(TurnState(content=[_post_block("C1:1717.5")]), row)
    assert not agent_posted_to(TurnState(content=[_post_block("C1")]), row), (
        "a top-level post in the channel is not the thread"
    )


def test_protected_controls_do_not_invite_a_post() -> None:
    text = render_routine_controls(_row(), platform="discord", direct_post="protected")
    assert "do not post there" in text
    assert "posts the end of your final reply there" not in text
    unverified = render_routine_controls(_row(), platform="discord", direct_post="unverified")
    assert "Do not post to the destination yourself" in unverified
    assert "posts the end of your final reply there" not in unverified


@pytest.mark.parametrize(
    ("platform", "kind", "destination_id", "ok"),
    [
        ("discord", "channel", "123456", True),
        ("discord", "thread", "123456", True),
        ("discord", "channel", "general", False),
        ("slack", "channel", "C0123ABC", True),
        ("slack", "channel", "C0123ABC:1.2", False),
        ("slack", "thread", "C0123ABC:1717171717.123456", True),
        ("slack", "thread", "C0123ABC", False),
        ("slack", "channel", "#general", False),
        ("teams", "channel", "x", False),
        ("teams", "channel", "19:abc-1@thread.tacv2", True),
        ("teams", "channel", "19:abc@thread.tacv2;messageid=17", False),
        ("teams", "thread", "19:abc@thread.tacv2;messageid=17", True),
        ("teams", "thread", "19:abc@thread.tacv2;messageid=x/../", False),
        ("cli", "channel", "x", False),
    ],
)
def test_destination_shape(platform: str, kind: str, destination_id: str, ok: bool) -> None:
    assert (destination_shape_error(platform, kind, destination_id) is None) is ok


def test_the_fallback_post_carries_the_tail() -> None:
    assert render_fallback_post(_row()).endswith("\n\nAll green.")


def test_the_fallback_post_names_the_agent_unless_the_post_carries_its_identity() -> None:
    row = _row(agent_name="research", cron_expr="0 17 * * 5", timezone="Europe/London")
    assert render_fallback_post(row) == (
        "Routine result from research (0 17 * * 5, Europe/London):\n\nAll green."
    )
    assert render_fallback_post(row, as_agent=True) == (
        "Routine result (0 17 * * 5, Europe/London):\n\nAll green."
    ), "the agent's own header says who it is from; the schedule stays"


def _identity_settings(*, enabled: bool) -> Settings:
    return Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://test:test@localhost/daimon_test"},
            "anthropic": {"api_key": "test"},
            "agent_identity": {"enabled": enabled},
        }
    )


async def test_routine_identity_is_plain_when_identity_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lookup(*args: object, **kwargs: object) -> None:
        raise AssertionError("identity off needs no agent lookup")

    monkeypatch.setattr(routine_delivery_module, "find_agent_by_daimon_tag", no_lookup)
    identity = await resolve_routine_identity(
        MagicMock(),
        MagicMock(),
        _identity_settings(enabled=False),
        row=_row(agent_name="research"),
        platform="slack",
        workspace_id="T1",
        default_agent_name="daimon",
    )
    assert identity == AgentIdentity(name="research", avatar_url=None, builtin=True)


async def test_routine_identity_resolves_the_agents_face_and_waits_for_it(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def lookup(*args: object, **kwargs: object) -> None:
        return None

    async def resolve(session: object, **kwargs: object) -> AgentIdentity:
        calls.append(kwargs)
        return AgentIdentity(name="research", avatar_url="https://app/a.png", builtin=False)

    monkeypatch.setattr(routine_delivery_module, "find_agent_by_daimon_tag", lookup)
    monkeypatch.setattr(routine_delivery_module, "resolve_agent_identity", resolve)
    row = _row(agent_name="research")
    identity = await resolve_routine_identity(
        db_session_factory,
        MagicMock(),
        _identity_settings(enabled=True),
        row=row,
        platform="discord",
        workspace_id="123",
        default_agent_name="daimon",
    )
    assert identity.avatar_url == "https://app/a.png"
    (call,) = calls
    assert call["is_builtin"] is False and call["wait_for_face"] is True
    assert call["tenant_id"] == row.tenant_id


async def test_routine_identity_falls_back_to_plain_when_the_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(*args: object, **kwargs: object) -> None:
        raise anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.test"))

    monkeypatch.setattr(routine_delivery_module, "find_agent_by_daimon_tag", failing)
    identity = await resolve_routine_identity(
        MagicMock(),
        MagicMock(),
        _identity_settings(enabled=True),
        row=_row(agent_name="research"),
        platform="slack",
        workspace_id="T1",
        default_agent_name="daimon",
    )
    assert identity.builtin, "a failed lookup posts the way it always has"


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
        # Creator before protection: a revoked creator on a protected
        # destination is `invoker_not_allowed`, never `protected_channel`
        # (which a poster may answer with a DM).
        (
            TenantAccessPolicy(protected_channel_ids=("C1",), invoker_user_ids=("U2",)),
            None,
            None,
            "U1",
            False,
            "invoker_not_allowed",
        ),
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


async def test_clear_creator_returns_the_policy_or_why_not(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daimon.core.routine_delivery as delivery_mod
    from daimon.core.stores.access_policy import AccessPolicyUnreadable

    row = await _routine(db_session)
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_category_ids=("CAT",)),
    )
    await db_session.commit()

    cleared = await clear_creator(db_session_factory, row, platform="discord")
    assert isinstance(cleared, TenantAccessPolicy)
    assert {c: r.writers for c, r in cleared.category_rules.items()} == {"CAT": "none"}

    await set_access_policy(
        db_session, tenant_id=row.tenant_id, policy=TenantAccessPolicy(invoker_user_ids=("U9",))
    )
    await db_session.commit()
    assert await clear_creator(db_session_factory, row, platform="discord") == (
        "invoker_not_allowed"
    )

    async def unreadable(*args: object, **kwargs: object) -> TenantAccessPolicy:
        raise AccessPolicyUnreadable(tenant_id=row.tenant_id)

    monkeypatch.setattr(delivery_mod, "load_access_policy", unreadable)
    assert await clear_creator(db_session_factory, row, platform="discord") == (
        "access_policy_unreadable"
    )


@pytest.mark.parametrize(
    ("policy", "platform", "kind", "unsafe"),
    [
        (TenantAccessPolicy(), "discord", "thread", False),
        (TenantAccessPolicy(protected_channel_ids=("P",)), "discord", "thread", True),
        (TenantAccessPolicy(protected_category_ids=("K",)), "discord", "thread", True),
        (TenantAccessPolicy(protected_category_ids=("K",)), "discord", "channel", True),
        (TenantAccessPolicy(protected_channel_ids=("P",)), "discord", "channel", False),
        (TenantAccessPolicy(protected_channel_ids=("P",)), "slack", "thread", False),
    ],
)
def test_placement_unknown_is_unsafe(
    policy: TenantAccessPolicy, platform: str, kind: str, unsafe: bool
) -> None:
    assert placement_unknown_is_unsafe(policy, platform=platform, kind=kind) is unsafe


_BASE = {
    "administrator": False,
    "view_channel": True,
    "send_messages": True,
    "is_thread": False,
    "send_messages_in_threads": True,
    "is_private_thread": False,
    "manage_threads": False,
    "is_thread_member": False,
}


@pytest.mark.parametrize(
    ("overrides", "allowed"),
    [
        ({}, True),
        ({"view_channel": False}, False),
        ({"send_messages": False}, False),
        ({"send_messages": False, "administrator": True}, True),
        ({"is_thread": True}, True),
        ({"is_thread": True, "send_messages_in_threads": False}, False),
        ({"is_thread": True, "send_messages": False}, True),  # threads use in-threads
        ({"is_thread": True, "is_private_thread": True}, False),
        ({"is_thread": True, "is_private_thread": True, "is_thread_member": True}, True),
        ({"is_thread": True, "is_private_thread": True, "manage_threads": True}, True),
    ],
)
def test_discord_creator_may_post(overrides: dict[str, bool], allowed: bool) -> None:
    from daimon.core.routine_delivery import discord_creator_may_post

    assert discord_creator_may_post(**{**_BASE, **overrides}) is allowed


@pytest.mark.parametrize(
    ("is_im_or_mpim", "is_private", "is_guest", "is_member", "allowed"),
    [
        (False, False, False, False, True),
        (False, True, False, False, False),
        (False, True, False, True, True),
        (False, False, True, False, False),
        (False, False, True, True, True),
        (True, False, False, True, False),
    ],
)
def test_slack_creator_may_post(
    is_im_or_mpim: bool, is_private: bool, is_guest: bool, is_member: bool, allowed: bool
) -> None:
    from daimon.core.routine_delivery import slack_creator_may_post

    assert (
        slack_creator_may_post(
            is_im_or_mpim=is_im_or_mpim,
            is_private=is_private,
            is_guest=is_guest,
            is_member=is_member,
        )
        is allowed
    )

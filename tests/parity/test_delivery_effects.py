"""Ordered delivery traces from the real turn entry points and fake platform APIs."""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import Platform
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import build_turn_router
from .drivers.effects import DeliveryEffect
from .drivers.protocol import PlatformDriver, platform_ids

# Current production differences. Slice 2/3 should remove entries, not add
# per-driver expected strings to the scenarios below.
DELIVERY_ALLOW_LIST: dict[str, object] = {
    "discord_split_final_id_first_chunk": (
        "discord",
        "DiscordTurnLifecycle.final_message_id retains the first chunk (#496)",
    ),
    "teams_controls_separate_post": (
        "teams",
        "Teams sends the cost line and feedback controls in a new card below the last chunk",
    ),
    "teams_failed_send_placeholder_final_id": (
        "teams",
        "Teams SDK supplies DO_NOT_USE_PLACEHOLDER_ID after a rejected send",
    ),
    "balance_depleted_notice_words": {
        "discord": "This server's daimon credit is depleted. A server admin can top up with `/billing`.",
        "slack": "This workspace's daimon credit is depleted. A workspace admin can top up with `/billing`.",
        "teams": (
            "This organisation's credit is depleted. An admin can top up with `billing` "
            "in a 1:1 chat with me."
        ),
    },
    "identity_builtin_and_discord_fallback": (
        "Discord uses a name prefix when its webhook cannot be provisioned; "
        "Slack and Teams treat the configured test agent as built-in"
    ),
}


def _answer_effects(effects: list[DeliveryEffect], scenario: str) -> list[DeliveryEffect]:
    marker = "Answer." if scenario == "one" else "word "
    return [e for e in effects if e.kind in ("send", "edit") and marker in (e.text or "")]


@pytest.mark.parametrize("identity_enabled", [False, True], ids=["identity-off", "identity-on"])
@pytest.mark.parametrize("scenario", ["one", "three"])
async def test_ordered_answer_delivery(
    driver: PlatformDriver,
    scenario: str,
    identity_enabled: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900001001, user=555000111, channel=100000
    )
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()
    lengths = {"discord": 4500, "slack": 25000, "teams": 9000}
    answer = "Answer. " if scenario == "one" else "word " * (lengths[driver.param_id] // 5)
    await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=build_turn_router(str(tenant.id), agent_text=answer),
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="hello",
        identity_enabled=identity_enabled,
    )
    effects = driver.captured_turn_effects()
    answers = _answer_effects(effects, scenario)
    assert len(answers) == (1 if scenario == "one" else 3), effects
    assert all(e.success for e in effects)
    assert all(e.message_id for e in answers)

    # Read the final state of each post. Discord first shows the summary on a
    # done card, then clears it as that card becomes chunk 1.
    current = {e.message_id: e for e in effects if e.kind in ("send", "edit")}
    summary_posts = [e for e in current.values() if e.summary is not None]
    assert len(summary_posts) == 1, summary_posts
    summary_post = summary_posts[0]
    last_chunk = answers[-1]
    if (
        driver.param_id
        == cast(tuple[str, str], DELIVERY_ALLOW_LIST["teams_controls_separate_post"])[0]
    ):
        assert summary_post.message_id != last_chunk.message_id
        assert effects.index(summary_post) > effects.index(last_chunk)
        assert summary_post.feedback
    else:
        assert summary_post.message_id == last_chunk.message_id
        if driver.param_id == "discord":
            reactions = [e for e in effects if e.kind == "react" and e.message_id != "trigger"]
            assert len(reactions) == 3
            assert {e.message_id for e in reactions} == {last_chunk.message_id}
        else:
            assert summary_post.feedback

    final_id = driver.recorded_final_message_id()
    if (
        scenario == "three"
        and driver.param_id
        == cast(tuple[str, str], DELIVERY_ALLOW_LIST["discord_split_final_id_first_chunk"])[0]
    ):
        assert final_id == answers[0].message_id
    else:
        assert final_id == last_chunk.message_id


@pytest.mark.parametrize("identity_enabled", [False, True], ids=["identity-off", "identity-on"])
async def test_send_failure_after_second_chunk_does_not_claim_full_delivery(
    driver: PlatformDriver,
    identity_enabled: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900001001, user=555000111, channel=100000
    )
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()
    lengths = {"discord": 4500, "slack": 25000, "teams": 9000}
    await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=build_turn_router(
            str(tenant.id), agent_text="word " * (lengths[driver.param_id] // 5)
        ),
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="hello",
        identity_enabled=identity_enabled,
        fail_answer_at=3,
    )
    effects = driver.captured_turn_effects()
    assert len([e for e in effects if not e.success]) == 1
    assert [e.success for e in _answer_effects(effects, "three")] == [True, True, False]
    if (
        driver.param_id
        == cast(tuple[str, str], DELIVERY_ALLOW_LIST["teams_failed_send_placeholder_final_id"])[0]
    ):
        # The SDK's failed POST returns a placeholder, not a confirmed message.
        assert driver.recorded_final_message_id() == "DO_NOT_USE_PLACEHOLDER_ID"
    else:
        assert driver.recorded_final_message_id() is None


@pytest.mark.parametrize("identity_enabled", [False, True], ids=["identity-off", "identity-on"])
async def test_generated_file_is_posted_once_after_three_chunks(
    driver: PlatformDriver,
    identity_enabled: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900001001, user=555000111, channel=100000
    )
    if driver.param_id == "teams":
        channel_id = "personal-parity"
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()
    lengths = {"discord": 4500, "slack": 25000, "teams": 9000}
    await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=build_turn_router(
            str(tenant.id),
            agent_text="word " * (lengths[driver.param_id] // 5),
            generated_file=("report.txt", b"report contents"),
        ),
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="make a report",
        identity_enabled=identity_enabled,
    )
    effects = driver.captured_turn_effects()
    assert len(_answer_effects(effects, "three")) == 3
    file_posts = [e for e in effects if "report.txt" in e.files]
    assert len(file_posts) == 1, file_posts
    assert file_posts[0].success
    if driver.param_id == "discord":
        assert file_posts[0].message_id == _answer_effects(effects, "three")[-1].message_id


@pytest.mark.parametrize("identity_enabled", [False, True], ids=["identity-off", "identity-on"])
async def test_cold_top_level_mention_opens_thread_and_answers(
    driver: PlatformDriver,
    identity_enabled: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900001001, user=555000111, channel=100000
    )
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()
    await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=build_turn_router(str(tenant.id), agent_text="Answer."),
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="hello",
        identity_enabled=identity_enabled,
        cold_thread=True,
    )
    effects = driver.captured_turn_effects()
    assert len(_answer_effects(effects, "one")) == 1
    assert driver.recorded_final_message_id() == _answer_effects(effects, "one")[0].message_id


@pytest.mark.parametrize("identity_enabled", [False, True], ids=["identity-off", "identity-on"])
async def test_budget_notice_copy_is_listed_in_one_allow_list(
    driver: PlatformDriver,
    identity_enabled: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, channel_id = platform_ids(
        driver.param_id, workspace=900001001, user=555000111, channel=100000
    )
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await db_session.commit()
    await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=build_turn_router(str(tenant.id)),
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="hello",
        identity_enabled=identity_enabled,
    )
    expected = cast(dict[str, str], DELIVERY_ALLOW_LIST["balance_depleted_notice_words"])[
        driver.param_id
    ]
    actual = "\n".join(
        (e.text or "") + "\n" + (e.summary or "") for e in driver.captured_turn_effects()
    )
    assert expected in actual

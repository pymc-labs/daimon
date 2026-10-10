"""Teams redelivery must never repeat a turn across worker lifetimes."""

import pytest
from daimon.core._models import TeamsActivityClaim
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.teams_activity_claims import claim
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_message_activity,
    patched_turns,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")


async def test_message_retry_after_worker_restart_does_not_run_second_turn(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    payload = make_message_activity(activity_id="same-after-restart")
    with patched_turns("done") as turns:
        for _ in range(2):
            async with running_service(
                build_teams_runtime(db_session_factory), teams_api_fake
            ) as service:
                await post_activity(service, payload)
                await service.turns.drain(timeout=30)
    assert len(turns) == 1


async def test_redelivery_of_unfinished_claim_does_not_run(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    payload = make_message_activity(activity_id="unfinished-after-restart")
    async with db_session_factory() as session, session.begin():
        assert await claim(
            session,
            tenant_id=derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID),
            conversation_id=CONVERSATION_ID,
            activity_id="unfinished-after-restart",
            thread_id=CONVERSATION_ID,
        )
    with patched_turns("done") as turns:
        async with running_service(
            build_teams_runtime(db_session_factory), teams_api_fake
        ) as service:
            await post_activity(service, payload)
            await service.turns.drain(timeout=30)
    assert turns == []


async def test_channel_claim_links_raw_delivery_to_normalized_thread_outcome(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    payload = make_message_activity(
        activity_id="channel-root",
        conversation_id="19:channel@thread.tacv2",
        conversation_type="channel",
        mention_bot=True,
    )
    with patched_turns("done") as turns:
        async with running_service(
            build_teams_runtime(db_session_factory), teams_api_fake
        ) as service:
            await post_activity(service, payload)
            await service.turns.drain(timeout=30)
    assert len(turns) == 1
    async with db_session_factory() as session:
        row = await session.scalar(
            select(TeamsActivityClaim).where(
                TeamsActivityClaim.conversation_id == "19:channel@thread.tacv2",
                TeamsActivityClaim.activity_id == "channel-root",
            )
        )
    assert row is not None
    assert row.thread_id == "19:channel@thread.tacv2;messageid=channel-root"
    assert row.outcome_id is not None
    assert row.finished_at is not None

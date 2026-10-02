"""Installs and removals through the real route: the team is recorded, welcomed, forgotten."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.teams_installations import get_teams_installation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    BOT_ACCOUNT_ID,
    ENTRA_TENANT_ID,
    SERVICE_URL,
    TEAM_GROUP_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_channel_activity,
    patched_turns,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")

TEAM = "19:team@thread.tacv2"
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


def _installation(action: str, *, tenant: str = ENTRA_TENANT_ID) -> dict[str, object]:
    return {
        "type": "installationUpdate",
        "action": action,
        "id": f"install-{uuid.uuid4()}",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:installer", "aadObjectId": str(uuid.UUID(int=9))},
        "recipient": {"id": BOT_ACCOUNT_ID, "name": "daimon"},
        "conversation": {
            "id": TEAM,
            "conversationType": "channel",
            "isGroup": True,
            "tenantId": tenant,
        },
        "channelData": {
            "tenant": {"id": tenant},
            "team": {"id": TEAM, "name": "Research", "aadGroupId": TEAM_GROUP_ID},
            "channel": {"id": TEAM},
        },
    }


async def _row(factory: async_sessionmaker[AsyncSession]) -> object:
    async with factory() as session:
        return await get_teams_installation(session, tenant_id=TENANT, team_id=TEAM)


async def test_an_install_records_the_team_and_welcomes_it_and_a_removal_forgets_it(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with running_service(build_teams_runtime(db_session_factory), teams_api_fake) as service:
        await post_activity(service, _installation("add"))
        row = await _row(db_session_factory)
        assert row is not None and row.group_id == TEAM_GROUP_ID and row.name == "Research"
        [welcome] = teams_api_fake.activity_requests
        assert f"/v3/conversations/{TEAM}/activities" in welcome.url
        assert "@mention me" in str(welcome.body["text"])

        await post_activity(service, _installation("remove"))
        assert await _row(db_session_factory) is None


async def test_another_organisations_install_records_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with running_service(build_teams_runtime(db_session_factory), teams_api_fake) as service:
        await post_activity(service, _installation("add", tenant=str(uuid.UUID(int=77))))
    assert await _row(db_session_factory) is None
    assert not teams_api_fake.activity_requests


async def test_a_channel_mention_records_a_team_installed_before_tracking(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """Teams installed before this release are found on their next activity."""
    with patched_turns():
        async with running_service(
            build_teams_runtime(db_session_factory), teams_api_fake
        ) as service:
            await post_activity(service, make_channel_activity())
            await service.turns.drain(timeout=30)
    row = await _row(db_session_factory)
    assert row is not None and row.group_id == TEAM_GROUP_ID, "resolved via team details"

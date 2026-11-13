"""Installs and removals through the real route: the team is recorded, welcomed, forgotten."""

from __future__ import annotations

import uuid

import pytest
from daimon.adapters.teams.installations import (
    chat_link,
    welcome_message,
    welcome_text,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.teams_installations import (
    get_teams_installation,
    list_teams_installations,
)
from daimon.core.stores.tenants import get_tenant, set_provision_status
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


def _installation(
    action: str, *, tenant: str = ENTRA_TENANT_ID, team: str = TEAM
) -> dict[str, object]:
    return {
        "type": "installationUpdate",
        "action": action,
        "id": f"install-{uuid.uuid4()}",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:installer", "aadObjectId": str(uuid.UUID(int=9))},
        "recipient": {"id": BOT_ACCOUNT_ID, "name": "daimon"},
        "conversation": {
            "id": team,
            "conversationType": "channel",
            "isGroup": True,
            "tenantId": tenant,
        },
        "channelData": {
            "tenant": {"id": tenant},
            "team": {"id": team, "name": "Research", "aadGroupId": TEAM_GROUP_ID},
            "channel": {"id": team},
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
        [card] = [a["content"] for a in welcome.body["attachments"]]
        assert "@mention me in a post or a reply to ask something." in str(card["body"])
        [action_set] = [e for e in card["body"] if e["type"] == "ActionSet"]
        assert action_set["actions"] == [
            {
                "type": "Action.OpenUrl",
                "title": "Open chat",
                "url": f"https://teams.microsoft.com/l/chat/0/0?users={BOT_ACCOUNT_ID}",
            }
        ], "one click opens the 1:1 chat the welcome names"
        assert "style" not in str(card["body"]), "a welcome is neutral, not red"

        await post_activity(service, _installation("remove"))
        assert await _row(db_session_factory) is None


async def _archived(factory: async_sessionmaker[AsyncSession]) -> bool:
    async with factory() as session:
        tenant = await get_tenant(session, TENANT)
    assert tenant is not None, "the tenant row stays through removals"
    return tenant.archived_at is not None


async def test_removing_the_last_team_archives_the_tenant_and_an_install_restores_it(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    other = "19:other@thread.tacv2"
    async with running_service(build_teams_runtime(db_session_factory), teams_api_fake) as service:
        await post_activity(service, _installation("add"))
        await post_activity(service, _installation("add", team=other))
        await post_activity(service, _installation("remove"))
        assert not await _archived(db_session_factory), "another team still has the app"

        await post_activity(service, _installation("remove", team=other))
        assert await _archived(db_session_factory), "removal from the last team archives"

        await post_activity(service, _installation("add"))
        assert not await _archived(db_session_factory), "installing again clears the archive"


async def test_a_personal_install_welcomes_the_chat_with_a_neutral_card(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    chat = "a:1personal-chat"
    install = _installation("add")
    install["conversation"] = {
        "id": chat,
        "conversationType": "personal",
        "tenantId": ENTRA_TENANT_ID,
    }
    install["channelData"] = {"tenant": {"id": ENTRA_TENANT_ID}}
    async with running_service(build_teams_runtime(db_session_factory), teams_api_fake) as service:
        await post_activity(service, install)
    [welcome] = teams_api_fake.activity_requests
    [card] = [a["content"] for a in welcome.body["attachments"]]
    [block] = card["body"]
    assert block["type"] == "TextBlock" and block["text"] == welcome_text("daimon", team=False)


def test_the_welcomes_say_what_to_do_in_the_place_they_are_sent() -> None:
    assert welcome_text("Daimon", team=False) == (
        "👋 I'm Daimon. I help with questions and data.\n\n"
        "Ask a question or send me a data file.\n\n"
        'To set things up, say "help me set up". '
        "Send `help` for the rest."
    )
    assert welcome_text("Daimon", team=True) == (
        "👋 I'm Daimon. I answer in the thread where you mention me.\n\n"
        "@mention me in a post or a reply to ask something.\n\n"
        'For setup, open a chat with me and say "help me set up".'
    )


@pytest.mark.parametrize(
    ("bot_id", "link"),
    [
        ("28:abc-123", "https://teams.microsoft.com/l/chat/0/0?users=28:abc-123"),
        ("29:a-person", None),
        ("", None),
        (None, None),
    ],
)
def test_the_chat_link_is_built_only_from_a_bot_account_id(
    bot_id: str | None, link: str | None
) -> None:
    assert chat_link(bot_id) == link


def test_a_team_welcome_without_a_bot_id_is_a_neutral_card_without_a_button() -> None:
    message = welcome_message("Daimon", team=True, bot_id=None)
    [attachment] = message.attachments or []
    [block] = attachment.content.body
    assert block.text == welcome_text("Daimon", team=True)


async def test_a_group_chat_install_posts_no_welcome(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    chat = "19:group@thread.v2"
    install = _installation("add")
    install["conversation"] = {
        "id": chat,
        "conversationType": "groupChat",
        "tenantId": ENTRA_TENANT_ID,
    }
    install["channelData"] = {"tenant": {"id": ENTRA_TENANT_ID}}
    async with running_service(build_teams_runtime(db_session_factory), teams_api_fake) as service:
        await post_activity(service, install)
    assert teams_api_fake.activity_requests == []


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


def _chat_install(action: str, chat: str) -> dict[str, object]:
    install = _installation(action)
    install["conversation"] = {
        "id": chat,
        "conversationType": "personal",
        "tenantId": ENTRA_TENANT_ID,
    }
    install["channelData"] = {"tenant": {"id": ENTRA_TENANT_ID}}
    return install


async def test_a_one_to_one_chat_keeps_the_tenant_unarchived_until_it_is_removed_too(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    chat = "a:1personal-chat"
    async with running_service(build_teams_runtime(db_session_factory), teams_api_fake) as service:
        await post_activity(service, _installation("add"))
        await post_activity(service, _chat_install("add", chat))
        await post_activity(service, _installation("remove"))
        assert not await _archived(db_session_factory), "a 1:1 chat still has the app"
        async with db_session_factory() as session:
            assert await list_teams_installations(session, tenant_id=TENANT) == [], (
                "a chat is never listed as a team"
            )

        await post_activity(service, _chat_install("remove", chat))
        assert await _archived(db_session_factory), "removing the last installation archives"


async def test_activity_from_an_archived_organisation_clears_the_archive(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """A tenant archived while still in use (nothing recorded for it) comes back on its
    next message, before the archive can age into retirement."""
    await set_provision_status(db_session_factory, tenant_id=TENANT, archive=True)
    assert await _archived(db_session_factory), "the tenant starts archived"
    with patched_turns():
        async with running_service(
            build_teams_runtime(db_session_factory), teams_api_fake
        ) as service:
            await post_activity(service, make_channel_activity())
            await service.turns.drain(timeout=30)
    assert not await _archived(db_session_factory), "inbound activity clears the archive"

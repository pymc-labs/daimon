"""The `here` command, through the real SDK route against a fake MA."""

from __future__ import annotations

import asyncio
import json

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.teams.here import here_card
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.here_card import assemble_here_card
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    CHANNEL_ID,
    DIRECT_CHAT_ID,
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_channel_activity,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


async def _anthropic() -> AsyncAnthropic:
    client = build_fake_anthropic(make_fake_ma_handler())
    await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(TENANT), "daimon_name": "daimon"},
    )
    return client


async def _cards_in_chat(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    activities: list[dict[str, object]],
) -> list[str]:
    """Post each activity; the JSON of every card answered in the 1:1 chat, in order."""
    runtime = build_teams_runtime(db_factory, anthropic=await _anthropic())
    async with running_service(runtime, fake) as service:
        for n, activity in enumerate(activities, start=1):
            await post_activity(service, activity)
            async with asyncio.timeout(10):
                while len(_chat(fake)) < n:
                    await asyncio.sleep(0.01)
    return _chat(fake)


def _chat(fake: TeamsApiFake) -> list[str]:
    return [
        json.dumps(r.body, ensure_ascii=False)
        for r in fake.activity_requests
        if f"/conversations/{DIRECT_CHAT_ID}/" in r.url and "attachments" in r.body
    ]


async def test_here_typed_in_a_channel_describes_that_channel_in_the_chat(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with db_session_factory.begin() as session:
        policy = TenantAccessPolicy(channel_rules={CHANNEL_ID: ChannelRule(readers="inside")})
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    activities = [
        make_channel_activity(text="here", activity_id="a-1"),
        make_message_activity(text="here", activity_id="a-2", conversation_id=DIRECT_CHAT_ID),
    ]
    in_channel, in_chat = await _cards_in_chat(db_session_factory, teams_api_fake, activities)

    assert "daimon answers" in in_channel, "the card names the answering agent"
    assert "Reading: Conversations here only" in in_channel, (
        "typed in a channel: that channel's reader rule"
    )
    assert "Reading: Any conversation" in in_chat, "typed in the chat: the chat's own rule"


def test_the_here_card_shows_names_as_literal_text() -> None:
    card = assemble_here_card(
        channel_id=CHANNEL_ID,
        platform="teams",
        agent_name="**bold** <at>x</at>",
        tier="channel",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(),
        details=None,
    )
    rendered = json.dumps(here_card(card).model_dump(by_alias=True, exclude_none=True))

    assert "\\\\*\\\\*bold\\\\*\\\\*" in rendered, "markdown in a name is escaped"
    assert "<at>" not in rendered, "a name cannot become a mention"

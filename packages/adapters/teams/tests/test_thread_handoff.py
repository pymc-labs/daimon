"""The Hand over button on a Teams conversation whose channel now answers with another agent."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.thread_handoff import VERB
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.turn.errors import SessionAgentMismatch
from daimon.testing import ma_agent
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    FakeSender,
    TeamsApiFake,
    assert_card_renders,
    bot_token,
    build_teams_runtime,
    make_card_action,
    make_inbound,
    patched_admission,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
_DEFAULT = DeploymentDefault(agent_name="daimon", environment_name="default")


def _click() -> dict[str, object]:
    """A Hand over click on the notice, a reply in a channel thread."""
    invoke = make_card_action(VERB, "hand_over", agent="ag_research")
    invoke["conversation"] = {
        "id": THREAD_ID,
        "conversationType": "channel",
        "tenantId": ENTRA_TENANT_ID,
    }
    invoke["channelData"] = {
        "tenant": {"id": ENTRA_TENANT_ID},
        "channel": {"id": CHANNEL_ID},
        "team": {"id": "19:team@thread.tacv2"},
    }
    return invoke


async def _hand_over(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    *,
    policy: TenantAccessPolicy | None = None,
    click: dict[str, object] | None = None,
    channel_id: str = CHANNEL_ID,
    thread_id: str = THREAD_ID,
) -> tuple[Any, str | None]:
    """Click Hand over; the invoke's answer and the thread's responder afterwards."""
    async with db_factory.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=TENANT, channel_id=channel_id),
            tenant_id=TENANT,
            agent_name="research-bot",
        )
        if policy is not None:
            await set_access_policy(session, tenant_id=TENANT, policy=policy)
    router = MARouter()
    router.add_agent(ma_agent(id="ag_research", name="research-bot", tenant_id=TENANT))
    runtime = build_teams_runtime(
        db_factory, anthropic=build_fake_anthropic(router.dispatch), deployment_default=_DEFAULT
    )
    async with running_service(runtime, fake) as service:
        answer = await post_activity(service, click or _click())
    async with db_factory() as session:
        routed = await resolve(
            session,
            context=ScopeContext(
                tenant_id=TENANT, channel_id=channel_id, thread_id=thread_id, platform="teams"
            ),
            default=_DEFAULT,
        )
    return answer, routed.responder_ma_agent_id if routed.thread_binding_id else None


async def test_the_responder_changed_notice_offers_hand_over(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={},
        bot_token=bot_token,
    )
    mismatch = SessionAgentMismatch(
        mapping_id=uuid.uuid4(), session_id="s", source_agent_id="a", destination_agent_id="b"
    )
    with (
        patched_admission(),
        patch.object(app_module, "bind_session", AsyncMock(side_effect=mismatch)),
    ):
        await teams._run_turn(make_inbound("w"), TENANT)  # pyright: ignore[reportPrivateUsage]

    content = sender.activities[-1].model_dump(by_alias=True, exclude_none=True)["attachments"][0][
        "content"
    ]
    assert_card_renders(content)
    notice = json.dumps(content)
    assert "Press Hand over" in notice, "the notice says what the button does"
    assert f'"verb": "{VERB}"' in notice, "the button routes to the hand-over handler"


async def test_a_click_hands_the_thread_to_the_channels_agent(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    answer, responder = await _hand_over(db_session_factory, teams_api_fake)

    assert "handed this conversation over" in json.dumps(answer), "everyone sees who did it"
    assert '"verb"' not in json.dumps(answer), "the replaced card loses its button"
    assert responder == "ag_research", "later messages go to research-bot"


async def test_a_refused_click_answers_privately_and_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    answer, responder = await _hand_over(
        db_session_factory,
        teams_api_fake,
        policy=TenantAccessPolicy(protected_channel_ids=(CHANNEL_ID,)),
    )

    assert "Nothing was changed" in json.dumps(answer), "only the clicker is told why"
    assert responder is None, "no binding was written"


async def test_a_click_in_a_1_1_chat_hands_over_the_chat(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    _, responder = await _hand_over(
        db_session_factory,
        teams_api_fake,
        click=make_card_action(VERB, "hand_over", agent="ag_research"),
        channel_id=CONVERSATION_ID,
        thread_id=CONVERSATION_ID,
    )

    assert responder == "ag_research", "the chat itself is the conversation handed over"


async def test_a_channel_click_without_its_notice_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    click = _click()
    del click["replyToId"]
    answer, responder = await _hand_over(db_session_factory, teams_api_fake, click=click)

    assert "something went wrong" in json.dumps(answer), "no thread to name, so it fails"
    assert responder is None, "no binding was written"

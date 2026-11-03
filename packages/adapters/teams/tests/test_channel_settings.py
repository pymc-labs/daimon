"""The Channel settings dialog: who may change which channel, and what each save stores.

Driven through the real SDK route; only Bot Framework and MA are faked.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from daimon.adapters.teams import channel_settings
from daimon.adapters.teams.channel_settings_card import (
    CHANNEL_DIALOG,
    MAX_LISTED_SKILLS,
    ChannelSettings,
    channel_settings_form,
)
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.domain import ChannelSkillRow
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.security_audit import list_events
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    MARouter,
    build_fake_anthropic,
    list_response,
    make_fake_ma_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    ADMIN_TEAM,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    TeamsApiFake,
    assert_card_renders,
    build_teams_runtime,
    make_card_action,
    make_invoke,
    notice_text,
    patched_turns,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
TEAM = "19:team@thread.tacv2"
GROWTH, LEGAL = "19:growth@thread.tacv2", "19:legal@thread.tacv2"
ADMIN, LEAD = AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID
NEW_ADMIN = "cccccccc-dddd-eeee-ffff-000000000000"
LIBRARY_SKILL = {
    "id": "skill_lib",
    "created_at": "2026-09-13T12:00:00Z",
    "display_title": tenant_scoped_display_title(tenant_id=TENANT, name="pdf-tools"),
    "latest_version": "v3",
    "source": "custom",
    "type": "skill",
    "updated_at": "2026-09-13T12:00:00Z",
}


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake
) -> AsyncIterator[TeamsHttpService]:
    """Run a two-channel team with a native server admin."""
    async with db_factory.begin() as session:
        await record_teams_installation(
            session, tenant_id=TENANT, team_id=TEAM, group_id=str(uuid.uuid4()), name="Lab"
        )
        for channel in (GROWTH, LEGAL):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=TENANT, channel_id=channel),
                tenant_id=TENANT,
                agent_name="analyst",
                mode="agent",
            )
    fake.channels.update({GROWTH: "Growth", LEGAL: "Legal"})
    runtime = build_teams_runtime(db_factory, anthropic=_anthropic(), admins=(ADMIN,))
    with patched_turns():
        async with running_service(runtime, fake) as service:
            yield service


def _anthropic() -> Any:
    state = FakeMAState()
    analyst = ma_agent(id="agent_analyst", tenant_id=TENANT, name="analyst")
    state.agents[analyst.id] = analyst.model_dump(mode="json")
    agents = make_fake_ma_handler(state)
    environments = MARouter()
    environments.add_environment_list(
        ma_environment(id="env_sci", name="science", tenant_id=TENANT)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/environments"):
            return environments.dispatch(request)
        if request.url.path == "/v1/skills":
            return list_response([LIBRARY_SKILL])
        return agents(request)

    return build_fake_anthropic(handler)


def _open(user: str) -> dict[str, object]:
    return make_invoke("task/fetch", {"data": {"dialog_id": CHANNEL_DIALOG}}, user=user)


def _submit(user: str, op: str, **fields: str) -> dict[str, object]:
    data: Mapping[str, object] = {"action": CHANNEL_DIALOG, "op": op} | fields
    return make_invoke("task/submit", {"data": dict(data)}, user=user)


async def _events(db_factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str, str]]:
    async with db_factory() as session:
        events = await list_events(session, tenant_id=TENANT)
    return [(e.tool_name, e.outcome, e.reason or "") for e in events]


def test_the_skills_dialog_says_when_more_exist_than_it_can_tick() -> None:
    """Past the cap the list folds and the ticks say how to reach the rest."""
    rows = tuple(
        ChannelSkillRow(
            tenant_id=TENANT,
            platform="teams",
            channel_id=GROWTH,
            skill_id=f"skill_{index}",
            version="v1",
            name=f"skill-{index}",
            owner_agent_name=None,
            added_by_account_id=None,
            added_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for index in range(MAX_LISTED_SKILLS + 1)
    )

    def form(skills: tuple[ChannelSkillRow, ...]) -> str:
        settings = ChannelSettings(
            channel_id=GROWTH,
            label="growth",
            picker=None,
            rule=None,
            skills=skills,
        )
        return json.dumps(
            channel_settings_form(settings).model_dump(by_alias=True), ensure_ascii=False
        )

    full, short = form(rows), form(rows[:MAX_LISTED_SKILLS])
    assert "…and 1 more" in full and "remove some to reach the rest" in full, "more exist"
    assert f"skill_{MAX_LISTED_SKILLS}" not in full, "only the first ones can be ticked"
    assert "reach the rest" not in short, "nothing more to reach"


async def test_a_server_admin_picks_a_channel_and_changes_environment_and_rule(
    service_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(service_session_factory, teams_api_fake) as service:
        picker = json.dumps(await post_activity(service, _open(ADMIN)))
        opened = await post_activity(service, _submit(ADMIN, "pick", channel=LEGAL))
        saved = await post_activity(
            service, _submit(ADMIN, "environment", channel=LEGAL, environment="env:science")
        )
        kept = await post_activity(
            service,
            _submit(ADMIN, "rule", channel=LEGAL, readers="any", writers="any", extra="copy"),
        )

    assert "Growth" in picker and "Legal" in picker, "a server admin picks any listed channel"
    assert_card_renders(opened["task"]["value"]["card"]["content"])
    form = json.dumps(opened)
    assert "Who can read it" in form, "and sees the rule control"
    assert "science environment" in json.dumps(saved)
    assert "Made legal, a copy of analyst, for this channel." in json.dumps(kept)
    async with service_session_factory() as session:
        scope = await get_scope(session, scope=ChannelScopeRef(tenant_id=TENANT, channel_id=LEGAL))
        policy = await load_access_policy(session, tenant_id=TENANT)
    assert scope is not None and scope.environment_name == "science"
    assert policy.channel_rules == {LEGAL: ChannelRule(readers="own", writers="own")}
    assert policy.agent_rules == {"legal": AgentRule(runs_in=(LEGAL,))}, "a copy named after it"
    assert await _events(service_session_factory) == [
        ("panel:environment", "allowed", "completed"),
        ("panel:channel_rule", "allowed", "completed"),
    ], "every write is audited"


async def test_a_server_admin_is_sent_to_chat_to_confirm_an_open_network(
    service_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """The form has no confirm step, so it writes nothing and points at chat, which asks."""
    async with service_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(channel_rules={GROWTH: ChannelRule(readers="inside")}),
        )
    async with _running(service_session_factory, teams_api_fake) as service:
        held = await post_activity(
            service, _submit(ADMIN, "environment", channel=GROWTH, environment="env:science")
        )

    assert "Ask me in chat to make this change for #Growth, then confirm when I ask." in json.dumps(
        held
    ), json.dumps(held)
    assert await _events(service_session_factory) == [
        ("panel:environment", "denied", "needs_confirm")
    ]


async def test_a_member_gets_no_dialog_and_no_button(
    service_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    member = "dddddddd-eeee-ffff-0000-111111111111"
    async with _running(service_session_factory, teams_api_fake) as service:
        refused = await post_activity(service, _open(member))
        routing = {
            user: json.dumps(
                await post_activity(service, make_card_action("agent_setup", "routing", user=user))
            )
            for user in (member, LEAD, ADMIN)
        }

    assert notice_text(refused) == channel_settings.channels_need_admin_message()
    assert "Channel settings" not in routing[member], "a member is offered nothing"
    assert "Channel settings" not in routing[LEAD]
    assert "Channel settings" in routing[ADMIN]


async def test_a_member_is_told_who_the_admins_are_when_the_admin_team_is_known(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    member = "dddddddd-eeee-ffff-0000-111111111111"
    line = "Admins are the owners of the Leads team."
    async with _running(db_session_factory, teams_api_fake) as service:
        async with db_session_factory.begin() as session:
            await record_teams_installation(
                session,
                tenant_id=TENANT,
                team_id="19:leads@thread.tacv2",
                group_id=ADMIN_TEAM,
                name="Leads",
            )
        opened = await post_activity(service, _open(member))
        submitted = await post_activity(service, _submit(member, "rule", channel=GROWTH))

    expected = f"{channel_settings.channels_need_admin_message()}\n\n{line}"
    assert notice_text(opened) == expected
    assert submitted["task"]["value"] == expected

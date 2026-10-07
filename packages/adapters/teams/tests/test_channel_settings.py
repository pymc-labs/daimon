"""The Channel settings dialog: who may change which channel, and what each save stores.

Driven through the real SDK route; only Bot Framework and MA are faked.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from daimon.adapters.teams import channel_settings
from daimon.adapters.teams.channel_settings_card import CHANNEL_DIALOG
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.channel_admins import get_channel_admins, set_channel_admins
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.security_audit import list_events
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.testing import build_fake_anthropic, ma_agent, ma_environment
from daimon.testing.ma import FakeMAState, MARouter, make_fake_ma_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    TeamsApiFake,
    assert_card_renders,
    build_teams_runtime,
    make_card_action,
    make_invoke,
    patched_turns,
    post_activity,
    running_service,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
TEAM = "19:team@thread.tacv2"
GROWTH, LEGAL = "19:growth@thread.tacv2", "19:legal@thread.tacv2"
ADMIN, LEAD = AAD_OBJECT_ID, OTHER_AAD_OBJECT_ID
NEW_ADMIN = "cccccccc-dddd-eeee-ffff-000000000000"


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
        return agents(request)

    return build_fake_anthropic(handler)


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake
) -> AsyncIterator[TeamsHttpService]:
    """ADMIN is a server admin; LEAD administers GROWTH; the team lists both channels."""
    async with db_factory.begin() as session:
        await record_teams_installation(
            session, tenant_id=TENANT, team_id=TEAM, group_id=str(uuid.uuid4()), name="Lab"
        )
        await set_channel_admins(
            session,
            tenant_id=TENANT,
            platform="teams",
            channel_id=GROWTH,
            role_ids=[],
            user_ids=[LEAD],
            actor_account_id=None,
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
    runtime = build_teams_runtime(
        db_factory, anthropic=_anthropic(), teams=teams_settings(admins=(ADMIN,))
    )
    with patched_turns():
        async with running_service(runtime, fake) as service:
            yield service


def _open(user: str) -> dict[str, object]:
    return make_invoke("task/fetch", {"data": {"dialog_id": CHANNEL_DIALOG}}, user=user)


def _submit(user: str, op: str, **fields: str) -> dict[str, object]:
    data: Mapping[str, object] = {"action": CHANNEL_DIALOG, "op": op} | fields
    return make_invoke("task/submit", {"data": dict(data)}, user=user)


async def _events(db_factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str, str]]:
    async with db_factory() as session:
        events = await list_events(session, tenant_id=TENANT)
    return [(e.tool_name, e.outcome, e.reason or "") for e in events]


async def test_a_server_admin_picks_a_channel_and_changes_all_three(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        picker = json.dumps(await post_activity(service, _open(ADMIN)))
        opened = await post_activity(service, _submit(ADMIN, "pick", channel=LEGAL))
        saved = await post_activity(
            service, _submit(ADMIN, "environment", channel=LEGAL, environment="env:science")
        )
        kept = await post_activity(
            service,
            _submit(ADMIN, "rule", channel=LEGAL, readers="any", writers="any", extra="copy"),
        )
        granted = await post_activity(
            service, _submit(ADMIN, "admins", channel=f"{LEGAL};messageid=1", admins=NEW_ADMIN)
        )

    assert "Growth" in picker and "Legal" in picker, "a server admin picks any listed channel"
    assert_card_renders(opened["task"]["value"]["card"]["content"])
    form = json.dumps(opened)
    assert "Who can read it" in form and "Entra object ids" in form, "and sees every control"
    assert "science environment" in json.dumps(saved)
    assert "legal, a copy of analyst, is its own agent" in json.dumps(kept)
    assert "Channel admins saved." in json.dumps(granted), "a thread id names its channel"
    async with db_session_factory() as session:
        scope = await get_scope(session, scope=ChannelScopeRef(tenant_id=TENANT, channel_id=LEGAL))
        policy = await load_access_policy(session, tenant_id=TENANT)
        grant = await get_channel_admins(
            session, tenant_id=TENANT, platform="teams", channel_id=LEGAL
        )
    assert scope is not None and scope.environment_name == "science"
    assert policy.channel_rules == {LEGAL: ChannelRule(readers="own", writers="own")}
    assert policy.agent_rules == {"legal": AgentRule(runs_in=(LEGAL,))}, "a copy named after it"
    assert grant is not None and grant.user_ids == (NEW_ADMIN,)
    assert await _events(db_session_factory) == [
        ("panel:environment", "allowed", "completed"),
        ("panel:channel_rule", "allowed", "completed"),
        ("panel:channel_admins", "allowed", "completed"),
    ], "every write is audited"


async def test_a_channel_admin_sets_only_their_own_channels_environment(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        picker = json.dumps(await post_activity(service, _open(LEAD)))
        form = json.dumps(await post_activity(service, _submit(LEAD, "pick", channel=GROWTH)))
        saved = await post_activity(
            service, _submit(LEAD, "environment", channel=GROWTH, environment="env:science")
        )
        elsewhere = await post_activity(
            service, _submit(LEAD, "environment", channel=LEGAL, environment="env:science")
        )
        typed = await post_activity(service, _submit(LEAD, "pick", channel_id=LEGAL))
        rule = await post_activity(
            service, _submit(LEAD, "rule", channel=GROWTH, readers="own", writers="own")
        )
        grant = await post_activity(
            service, _submit(LEAD, "admins", channel=GROWTH, admins=NEW_ADMIN)
        )

    assert "Growth" in picker and "Legal" not in picker, "only the channels they run"
    assert "Or a channel id" not in picker, "and no free entry"
    assert "Environment" in form and "Who can read it" not in form and "Entra" not in form
    assert "science environment" in json.dumps(saved), "their own channel's environment is theirs"
    assert elsewhere["task"]["value"] == channel_settings.CHANNELS_NEED_ADMIN
    assert "Who can read it" not in json.dumps(typed), "a typed id is a server admin's only"
    assert rule["task"]["value"] == channel_settings.SERVER_ADMIN_ONLY
    assert grant["task"]["value"] == channel_settings.SERVER_ADMIN_ONLY
    async with db_session_factory() as session:
        legal = await get_scope(session, scope=ChannelScopeRef(tenant_id=TENANT, channel_id=LEGAL))
        policy = await load_access_policy(session, tenant_id=TENANT)
        admins = await get_channel_admins(
            session, tenant_id=TENANT, platform="teams", channel_id=GROWTH
        )
    assert legal is not None and legal.environment_name is None, "the other channel is untouched"
    assert policy == TenantAccessPolicy(), "a forged rule writes nothing"
    assert admins is not None and admins.user_ids == (LEAD,), "nor a forged grant"
    assert await _events(db_session_factory) == [
        ("panel:environment", "allowed", "completed"),
        ("panel:environment", "denied", "needs_admin"),
        ("panel:channel_rule", "denied", "needs_admin"),
        ("panel:channel_admins", "denied", "needs_admin"),
    ]


async def test_a_channel_admin_puts_no_open_network_where_only_turns_inside_read(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """The same `authorize_environment_pick` rule as Discord and Slack."""
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(channel_rules={GROWTH: ChannelRule(readers="inside")}),
        )
    async with _running(db_session_factory, teams_api_fake) as service:
        refused = await post_activity(
            service, _submit(LEAD, "environment", channel=GROWTH, environment="env:science")
        )

    assert "unrestricted" in json.dumps(refused).lower(), json.dumps(refused)
    assert await _events(db_session_factory) == [
        ("panel:environment", "denied", "authz:not_a_reader")
    ]


async def test_a_server_admin_is_sent_to_chat_to_confirm_an_open_network(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """The form has no confirm step, so it writes nothing and points at chat, which asks."""
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(channel_rules={GROWTH: ChannelRule(readers="inside")}),
        )
    async with _running(db_session_factory, teams_api_fake) as service:
        held = await post_activity(
            service, _submit(ADMIN, "environment", channel=GROWTH, environment="env:science")
        )

    assert "confirm there" in json.dumps(held), json.dumps(held)
    assert await _events(db_session_factory) == [("panel:environment", "denied", "needs_confirm")]


async def test_a_member_gets_no_dialog_and_no_button(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    member = "dddddddd-eeee-ffff-0000-111111111111"
    async with _running(db_session_factory, teams_api_fake) as service:
        refused = await post_activity(service, _open(member))
        routing = {
            user: json.dumps(
                await post_activity(service, make_card_action("agent_setup", "routing", user=user))
            )
            for user in (member, LEAD, ADMIN)
        }

    assert refused["task"]["value"] == channel_settings.CHANNELS_NEED_ADMIN
    assert "Channel settings" not in routing[member], "a member is offered nothing"
    assert "Channel settings" in routing[LEAD] and "Channel settings" in routing[ADMIN]

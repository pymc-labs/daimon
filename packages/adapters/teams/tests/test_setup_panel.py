"""The `setup` panel and setup conversations, driven through the real SDK route.

Only the outbound Bot Framework transport, MA and the MA turn itself are faked.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from daimon.adapters.teams import setup_card, setup_panel
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.setup_card import ENDED
from daimon.core._models import AgentCreationChannel, ThreadSession
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef
from daimon.core.setup_conversations import setup_thread_name
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import ThreadAgentBindingRow
from daimon.core.stores.mcp_tokens import get_mcp_token, list_mcp_tokens
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.security_audit import list_events
from daimon.core.stores.thread_agent_bindings import list_active_bindings
from daimon.testing import build_fake_anthropic, ma_agent
from daimon.testing.ma import FakeMAState, make_fake_ma_handler
from pydantic import HttpUrl, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_card_action,
    make_invoke,
    make_message_activity,
    patched_turns,
    post_activity,
    running_service,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
DAIMON_ID, ANALYST_ID = "agent_daimon", "agent_analyst"


def _ma_state() -> FakeMAState:
    state = FakeMAState()
    managed = {MA_METADATA_KEY_MANAGED: "true"}
    for agent in (
        ma_agent(id=DAIMON_ID, tenant_id=TENANT, name="daimon", metadata=managed),
        ma_agent(id=ANALYST_ID, tenant_id=TENANT, name="analyst"),
        ma_agent(id="agent_test_id", name="usual"),  # the patched admission's pick
    ):
        state.agents[agent.id] = agent.model_dump(mode="json")
    return state


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    admins: tuple[str, ...] = (),
    state: FakeMAState | None = None,
) -> AsyncIterator[tuple[TeamsHttpService, list[dict[str, Any]]]]:
    ma = build_fake_anthropic(make_fake_ma_handler(state or _ma_state()))
    runtime = build_teams_runtime(db_factory, anthropic=ma, teams=teams_settings(admins=admins))
    runtime.settings.mcp.public_url = HttpUrl("https://mcp.example.test/mcp")
    runtime.settings.mcp.jwt_secret = SecretStr("s" * 48)
    with patched_turns() as turns:
        async with running_service(runtime, fake) as service:
            yield service, turns


async def _say(service: TeamsHttpService, text: str) -> None:
    await post_activity(service, make_message_activity(text=text, activity_id=f"a-{uuid.uuid4()}"))
    await service.turns.drain(timeout=30)
    service.turns.draining = False


def _click(op: str, *, user: str = AAD_OBJECT_ID, **fields: object) -> dict[str, object]:
    return make_card_action("agent_setup", op, user=user, **fields)


def _dialog(
    kind: str, data: Mapping[str, object], *, user: str = AAD_OBJECT_ID
) -> dict[str, object]:
    return make_invoke(f"task/{kind}", {"data": dict(data)}, user=user)


async def _live(db_factory: async_sessionmaker[AsyncSession]) -> list[ThreadAgentBindingRow]:
    async with db_factory() as session:
        return await list_active_bindings(
            session, tenant_id=TENANT, platform="teams", parent_channel_id=CONVERSATION_ID
        )


async def test_setup_lists_every_agent_with_the_panel_actions(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, _):
        await _say(service, "setup")

    card = json.dumps(teams_api_fake.activity_requests[-1].body)
    assert "daimon" in card and "analyst" in card
    assert "Answers in this chat" in card, "the deployment default answers the chat"
    assert "New agent" in card and "Who answers where" in card


async def test_details_and_routing_replace_the_card_and_a_gone_agent_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, _):
        details = await post_activity(service, _click("details", agent="analyst"))
        routing = await post_activity(service, _click("routing"))
        gone = await post_activity(service, _click("details", agent="deleted-one"))

    assert details["type"] == "application/vnd.microsoft.card.adaptive"
    assert "Model:" in json.dumps(details) and "Use from your coding tools" in json.dumps(details)
    assert "Organisation default" in json.dumps(routing)
    assert setup_panel.GONE in json.dumps(gone), "the roster returns with a notice"


@pytest.mark.parametrize("admin", [False, True], ids=["member", "admin"])
async def test_routing_hides_an_isolated_channels_environment_from_outside(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    admin: bool,
) -> None:
    """As on Discord and Slack: a member outside an isolated channel never sees its
    environment in Who answers where; an admin sees every channel's."""
    isolated, open_channel = "19:vault@thread.tacv2", "19:lobby@thread.tacv2"
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(
                sealed_channel_ids=(isolated,), isolated_channel_ids=(isolated,)
            ),
        )
        for channel, environment in ((isolated, "vault-env"), (open_channel, "lobby-env")):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=TENANT, channel_id=channel),
                tenant_id=TENANT,
                environment_name=environment,
            )
    admins = (AAD_OBJECT_ID,) if admin else ()
    async with _running(db_session_factory, teams_api_fake, admins=admins) as (service, _):
        routing = json.dumps(await post_activity(service, _click("routing")))

    assert "lobby-env" in routing, "an open channel's environment is listed"
    assert ("vault-env" in routing) is admin, "the isolated channel's shows only to an admin"


@pytest.mark.parametrize("admin", [False, True], ids=["member", "admin"])
async def test_the_agents_list_hides_an_isolated_channels_own_agent_from_outside(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    admin: bool,
) -> None:
    """The panel lives in the 1:1 chat, outside every channel: a member never sees
    an isolated channel's own agent there, nor opens its Details; an admin does."""
    isolated = "19:vault@thread.tacv2"
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(
                sealed_channel_ids=(isolated,),
                isolated_channel_ids=(isolated,),
                agent_channel_pins={"analyst": (isolated,)},
            ),
        )
    admins = (AAD_OBJECT_ID,) if admin else ()
    async with _running(db_session_factory, teams_api_fake, admins=admins) as (service, _):
        await _say(service, "setup")
        details = json.dumps(await post_activity(service, _click("details", agent="analyst")))

    agents = json.dumps(teams_api_fake.activity_requests[-1].body)
    assert "daimon" in agents, "the open agents are listed"
    assert ("analyst" in agents) is admin, "the isolated channel's own agent shows only to an admin"
    assert (setup_panel.GONE in details) is not admin, "a member can't open its Details"


async def test_manage_switches_the_chat_into_setup_until_new_ends_it(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        toast = await post_activity(service, _click("manage", agent=ANALYST_ID))
        [binding] = await _live(db_session_factory)
        await _say(service, "please add a skill")
        await _say(service, "new")
        after = await _live(db_session_factory)
        await _say(service, "back to normal")

    assert toast["value"] == setup_panel.STARTED
    assert binding.kind == "setup" and binding.responder_ma_agent_id == DAIMON_ID
    assert binding.configuration_target_ma_agent_id == ANALYST_ID
    assert binding.thread_id.startswith(f"{CONVERSATION_ID};setup=")
    assert setup_thread_name("analyst") in json.dumps(teams_api_fake.activity_requests[0].body)
    origins = [t["user_message"].replace(" ", "") for t in turns]
    assert ['"is_setup":true' in o for o in origins] == [True, False], "setup, then the usual"
    assert after == [], "new ends the setup conversation"
    assert any(ENDED in json.dumps(r.body) for r in teams_api_fake.activity_requests)
    async with db_session_factory() as session:
        keys = set((await session.execute(select(ThreadSession.thread_id))).scalars())
    assert keys == {binding.thread_id, CONVERSATION_ID}, "setup never touches the chat's session"


async def test_end_button_ends_once_and_reopening_replaces_the_live_conversation(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, _):
        await post_activity(service, _click("manage", agent=""))
        [first] = await _live(db_session_factory)
        await post_activity(service, _click("manage", agent=ANALYST_ID))
        [second] = await _live(db_session_factory)
        stale = await post_activity(service, _click("end", thread=first.thread_id))
        ended = await post_activity(service, _click("end", thread=second.thread_id))
        remaining = await _live(db_session_factory)

    assert first.thread_id != second.thread_id, "one live setup conversation per chat"
    assert setup_panel.ALREADY_ENDED in json.dumps(stale)
    assert ENDED in json.dumps(ended) and remaining == []


async def test_create_rejects_a_bad_name_then_creates_an_unrouted_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[tuple[uuid.UUID, str]] = []
    monkeypatch.setattr(
        setup_panel,
        "queue_agent_face",
        lambda _factory, *, tenant_id, agent_name, **_: queued.append((tenant_id, agent_name)),
    )
    state = _ma_state()
    form = {"action": "agent_create", "purpose": "Scout leads", "model": DEFAULT_AGENT_MODEL}
    async with _running(db_session_factory, teams_api_fake, state=state) as (service, _):
        opened = await post_activity(service, _dialog("fetch", {"dialog_id": "agent_create"}))
        bad = await post_activity(service, _dialog("submit", form | {"name": "bad name!"}))
        good = await post_activity(service, _dialog("submit", form | {"name": "scout"}))

    assert opened["task"]["type"] == "continue", "any member may open the form"
    assert bad["task"]["type"] == "continue" and "Name must be" in json.dumps(bad)
    assert good["task"]["value"].startswith("Created scout")
    [created] = [a for a in state.agents.values() if a["name"] == "scout"]
    assert "Scout leads" in str(created["system"])
    edits = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert edits and edits[-1].url.endswith("/activities/m-7"), "the panel lands on Details"
    assert queued == [(TENANT, "scout")], "the new agent's face renders when it is created"


@pytest.mark.parametrize(
    ("user", "admins", "theirs"),
    [
        (AAD_OBJECT_ID, (), True),
        (OTHER_AAD_OBJECT_ID, (), False),
        (AAD_OBJECT_ID, (AAD_OBJECT_ID,), False),
    ],
)
async def test_a_channel_admins_new_agent_is_their_channels(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    user: str,
    admins: tuple[str, ...],
    theirs: bool,
) -> None:
    """Only a channel admin, not a member or a server admin, makes it the channel's."""
    async with db_session_factory.begin() as session:
        await set_channel_admins(
            session,
            tenant_id=TENANT,
            platform="teams",
            channel_id=CONVERSATION_ID,
            role_ids=[],
            user_ids=[AAD_OBJECT_ID],
            actor_account_id=None,
        )
    form = {"action": "agent_create", "name": "room", "model": DEFAULT_AGENT_MODEL}
    async with _running(db_session_factory, teams_api_fake, admins) as (service, _):
        await post_activity(service, _dialog("submit", form, user=user))

    async with db_session_factory() as session:
        channels = (await session.scalars(select(AgentCreationChannel.channel_id))).all()
    assert channels == ([CONVERSATION_ID] if theirs else [])


async def test_coding_tools_mint_is_admin_only_and_only_the_minter_revokes(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    fetch = {"dialog_id": "agent_coding_tools", "agent": "analyst"}
    admins = (AAD_OBJECT_ID,)
    with structlog.testing.capture_logs() as logs:
        async with _running(db_session_factory, teams_api_fake, admins) as (service, _):
            refused = await post_activity(
                service, _dialog("fetch", fetch, user=OTHER_AAD_OBJECT_ID)
            )
            minted = await post_activity(service, _dialog("fetch", fetch))
            card = json.dumps(minted)
            jti = card.split('"jti": "')[1].split('"')[0]
            revoke = {"action": "agent_coding_tools", "jti": jti}
            other = await post_activity(
                service, _dialog("submit", revoke, user=OTHER_AAD_OBJECT_ID)
            )
            mine = await post_activity(service, _dialog("submit", revoke))
            again = await post_activity(service, _dialog("submit", revoke))

    assert refused["task"]["value"] == setup_panel.NEEDS_ADMIN.format(name="analyst")
    assert minted["task"]["type"] == "continue" and "claude mcp add" in card
    token = card.split("Bearer ")[1].split("\\")[0]
    assert token not in json.dumps(logs, default=str), "the token value is never logged"
    assert other["task"]["value"] == setup_panel.NOT_MINTER
    assert mine["task"]["value"] == "Token revoked."
    assert "already revoked" in again["task"]["value"]
    async with db_session_factory() as session:
        row = await get_mcp_token(session, jti=uuid.UUID(jti))
    assert row is not None and row.revoked_at is not None
    async with db_session_factory() as session:
        events = await list_events(session, tenant_id=TENANT)
    assert [(e.tool_name, e.outcome, e.reason) for e in events] == [
        ("panel:coding_token_mint", "denied", "authz:admin_required"),
        ("panel:coding_token_mint", "allowed", "completed"),
        ("panel:coding_token_revoke", "denied", "not_minter"),
        ("panel:coding_token_revoke", "allowed", "completed"),
        ("panel:coding_token_revoke", "error", "already_revoked"),
    ], "every mint and revoke, allowed or not, is audited"
    assert {e.token_jti for e in events[1:]} == {uuid.UUID(jti)}


PINNED, ELSEWHERE = "19:pinned@thread.tacv2", "19:elsewhere@thread.tacv2"


async def _grant_and_pin(db_factory: async_sessionmaker[AsyncSession], *, pin: bool) -> None:
    """The member administers PINNED; `pin` pins analyst there."""
    async with db_factory.begin() as session:
        await set_channel_admins(
            session,
            tenant_id=TENANT,
            platform="teams",
            channel_id=PINNED,
            role_ids=[],
            user_ids=[OTHER_AAD_OBJECT_ID],
            actor_account_id=None,
        )
        if pin:
            await set_access_policy(
                session,
                tenant_id=TENANT,
                policy=TenantAccessPolicy(agent_channel_pins={"analyst": (PINNED,)}),
            )


async def test_a_channel_admin_mints_a_token_bound_to_a_channel_they_pick(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """The dialog lists only their channels, with no unbound choice, and binds the token."""
    await _grant_and_pin(db_session_factory, pin=True)
    fetch = {"dialog_id": "agent_coding_tools", "agent": "analyst"}
    mint = {"action": "agent_coding_tools", "op": "mint", "agent": "analyst"}
    async with _running(db_session_factory, teams_api_fake) as (service, _):
        form = await post_activity(service, _dialog("fetch", fetch, user=OTHER_AAD_OBJECT_ID))
        minted = await post_activity(
            service, _dialog("submit", mint | {"channel": PINNED}, user=OTHER_AAD_OBJECT_ID)
        )
        forged = await post_activity(
            service, _dialog("submit", mint | {"channel": ELSEWHERE}, user=OTHER_AAD_OBJECT_ID)
        )
        unbound = await post_activity(
            service, _dialog("submit", mint | {"channel": "none"}, user=OTHER_AAD_OBJECT_ID)
        )

    choices = json.dumps(form)
    assert PINNED in choices and ELSEWHERE not in choices, "only the channels they administer"
    assert "Not bound to a channel" not in choices, "an unbound token stays with server admins"
    card = json.dumps(minted)
    assert "claude mcp add" in card and PINNED in card, "the token says where it runs"
    jti = card.split('"jti": "')[1].split('"')[0]
    async with db_session_factory() as session:
        row = await get_mcp_token(session, jti=uuid.UUID(jti))
    assert row is not None and (row.platform, row.channel_id) == ("teams", PINNED), (
        "the token is bound to the picked channel"
    )
    refusal = setup_panel.NEEDS_ADMIN.format(name="analyst")
    assert forged["task"]["value"] == refusal, "a channel they do not administer is refused"
    assert unbound["task"]["value"] == refusal, "and so is an unbound token"


async def test_a_channel_admin_cannot_mint_for_an_agent_pinned_nowhere_of_theirs(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    """An unpinned agent's token would be unbound, so it stays with server admins."""
    await _grant_and_pin(db_session_factory, pin=False)
    mint = {"action": "agent_coding_tools", "op": "mint", "agent": "analyst", "channel": PINNED}
    async with _running(db_session_factory, teams_api_fake) as (service, _):
        refused = await post_activity(service, _dialog("submit", mint, user=OTHER_AAD_OBJECT_ID))
    assert refused["task"]["value"] == setup_panel.NEEDS_ADMIN.format(name="analyst")


async def test_a_server_admin_with_a_channel_grant_keeps_the_unbound_choice(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await _grant_and_pin(db_session_factory, pin=False)
    fetch = {"dialog_id": "agent_coding_tools", "agent": "analyst"}
    mint = {"action": "agent_coding_tools", "op": "mint", "agent": "analyst", "channel": "none"}
    admins = (OTHER_AAD_OBJECT_ID,)
    async with _running(db_session_factory, teams_api_fake, admins) as (service, _):
        form = await post_activity(service, _dialog("fetch", fetch, user=OTHER_AAD_OBJECT_ID))
        minted = await post_activity(service, _dialog("submit", mint, user=OTHER_AAD_OBJECT_ID))
    assert "Not bound to a channel" in json.dumps(form), "admins may still mint unbound"
    card = json.dumps(minted)
    jti = card.split('"jti": "')[1].split('"')[0]
    async with db_session_factory() as session:
        row = await get_mcp_token(session, jti=uuid.UUID(jti))
    assert row is not None and row.channel_id is None, "the unbound pick mints an unbound token"


async def test_an_admin_mints_lists_and_revokes_operator_tokens_and_a_member_cannot(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    dialog_id = setup_card.OPERATOR_DIALOG
    mint = {"action": dialog_id, "op": "mint", "scopes": "tenant:read,promo:redeem", "label": "ci"}
    async with _running(db_session_factory, teams_api_fake, (AAD_OBJECT_ID,)) as (service, _):
        refused = await post_activity(service, _dialog("submit", mint, user=OTHER_AAD_OBJECT_ID))
        forged = await post_activity(service, _dialog("submit", mint | {"scopes": "promo:create"}))
        minted = await post_activity(service, _dialog("submit", mint))
        listing = await post_activity(service, _dialog("fetch", {"dialog_id": dialog_id}))
        async with db_session_factory() as session:
            (row,) = await list_mcp_tokens(session, now=datetime.now(UTC), tenant_id=TENANT)
        revoke = {"action": dialog_id, "op": "revoke", "jti": str(row.jti)}
        revoked = await post_activity(service, _dialog("submit", revoke))

    assert refused["task"]["value"] == setup_panel.OPERATOR_NEEDS_ADMIN
    assert "mint-operator-token" in json.dumps(forged), "the deployment scope stays with the CLI"
    assert "Shown once" in json.dumps(minted) and row.kind == "operator"
    assert set(row.scopes) == {"tenant:read", "promo:redeem"}
    listed = json.dumps(listing)
    assert str(row.jti)[:8] in listed and "Bearer" not in listed, "the listing has no secret"
    assert "Token revoked." in json.dumps(revoked)
    async with db_session_factory() as session:
        assert await list_mcp_tokens(session, now=datetime.now(UTC), tenant_id=TENANT) == []
        events = await list_events(session, tenant_id=TENANT)
    assert [(e.tool_name, e.outcome) for e in events] == [
        ("panel:operator_token_mint", "denied"),
        ("panel:operator_token_mint", "denied"),
        ("panel:operator_token_mint", "allowed"),
        ("panel:operator_token_revoke", "allowed"),
    ]

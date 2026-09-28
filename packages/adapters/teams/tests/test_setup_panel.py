"""The `setup` panel and setup conversations, driven through the real SDK route.

Only the outbound Bot Framework transport, MA and the MA turn itself are faked.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from daimon.adapters.teams import setup_panel
from daimon.adapters.teams.http_service import TeamsHttpService, create_teams_http_service
from daimon.adapters.teams.setup_card import ENDED
from daimon.core._models import ThreadSession
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.setup_conversations import setup_thread_name
from daimon.core.stores.domain import ThreadAgentBindingRow
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.stores.thread_agent_bindings import list_active_bindings
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing import build_fake_anthropic, ma_agent
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.ma import FakeMAState, make_fake_ma_handler
from pydantic import HttpUrl, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_ACCOUNT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
    patched_admission,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
DAIMON_ID, ANALYST_ID = "agent_daimon", "agent_analyst"


@pytest.fixture(autouse=True)
async def provisioned_tenant(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)


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
    settings = teams_settings(admins=admins)
    ma = build_fake_anthropic(make_fake_ma_handler(state or _ma_state()))
    runtime = build_teams_runtime(db_factory, anthropic=ma, teams=settings)
    runtime.settings.mcp.public_url = HttpUrl("https://mcp.example.test/mcp")
    runtime.settings.mcp.jwt_secret = SecretStr("s" * 48)
    service = create_teams_http_service(
        settings=settings, runtime=runtime, client=build_teams_client(fake)
    )
    turns: list[dict[str, Any]] = []

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        turns.append(kwargs)
        state = TurnState(content=[TextBlock(kind="text", text="On it.")])
        await lifecycle.on_terminal_success(state)
        return state

    with (
        patched_admission(),
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as run_turn,
    ):
        run_turn.side_effect = _fake_run_turn
        async with asgi_lifespan(service.app):
            await service.turns.start()
            yield service, turns


async def _post(service: TeamsHttpService, payload: dict[str, object]) -> Any:
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/messages", json=payload)
    assert response.status_code in (200, 201, 202), response.text
    return response.json() if response.content else None


async def _say(service: TeamsHttpService, text: str) -> None:
    await _post(service, make_message_activity(text=text, activity_id=f"a-{uuid.uuid4()}"))
    await service.turns.drain(timeout=30)
    service.turns.draining = False


def _invoke(name: str, value: dict[str, object], *, user: str = AAD_OBJECT_ID) -> dict[str, object]:
    return {
        "type": "invoke",
        "name": name,
        "id": f"invoke-{uuid.uuid4()}",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": f"29:{user}", "aadObjectId": user},
        "recipient": {"id": BOT_ACCOUNT_ID},
        "conversation": {
            "id": CONVERSATION_ID,
            "conversationType": "personal",
            "tenantId": ENTRA_TENANT_ID,
        },
        "replyToId": "m-7",
        "value": value,
    }


def _click(op: str, *, user: str = AAD_OBJECT_ID, **fields: object) -> dict[str, object]:
    data = {"action": "agent_setup", "op": op, **fields}
    action = {"type": "Action.Execute", "verb": "agent_setup", "data": data}
    return _invoke("adaptiveCard/action", {"action": action, "trigger": "manual"}, user=user)


def _dialog(
    kind: str, data: Mapping[str, object], *, user: str = AAD_OBJECT_ID
) -> dict[str, object]:
    return _invoke(f"task/{kind}", {"data": dict(data)}, user=user)


async def _live(db_factory: async_sessionmaker[AsyncSession]) -> list[ThreadAgentBindingRow]:
    async with db_factory() as session:
        return await list_active_bindings(
            session, tenant_id=TENANT, platform="teams", parent_channel_id=CONVERSATION_ID
        )


@pytest.mark.asyncio
async def test_setup_lists_every_agent_with_the_panel_actions(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, _):
        await _say(service, "setup")

    card = json.dumps(teams_api_fake.activity_requests[-1].body)
    assert "daimon" in card and "analyst" in card
    assert "Answers in this chat" in card, "the deployment default answers the chat"
    assert "New agent" in card and "Who answers where" in card


@pytest.mark.asyncio
async def test_details_and_routing_replace_the_card_and_a_gone_agent_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, _):
        details = await _post(service, _click("details", agent="analyst"))
        routing = await _post(service, _click("routing"))
        gone = await _post(service, _click("details", agent="deleted-one"))

    assert details["type"] == "application/vnd.microsoft.card.adaptive"
    assert "Model:" in json.dumps(details) and "Use from your coding tools" in json.dumps(details)
    assert "Workspace default" in json.dumps(routing)
    assert setup_panel.GONE in json.dumps(gone), "the roster returns with a notice"


@pytest.mark.asyncio
async def test_manage_switches_the_chat_into_setup_until_new_ends_it(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, turns):
        toast = await _post(service, _click("manage", agent=ANALYST_ID))
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


@pytest.mark.asyncio
async def test_end_button_ends_once_and_reopening_replaces_the_live_conversation(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as (service, _):
        await _post(service, _click("manage", agent=""))
        [first] = await _live(db_session_factory)
        await _post(service, _click("manage", agent=ANALYST_ID))
        [second] = await _live(db_session_factory)
        stale = await _post(service, _click("end", thread=first.thread_id))
        ended = await _post(service, _click("end", thread=second.thread_id))
        remaining = await _live(db_session_factory)

    assert first.thread_id != second.thread_id, "one live setup conversation per chat"
    assert setup_panel.ALREADY_ENDED in json.dumps(stale)
    assert ENDED in json.dumps(ended) and remaining == []


@pytest.mark.asyncio
async def test_create_rejects_a_bad_name_then_creates_an_unrouted_agent(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    state = _ma_state()
    form = {"action": "agent_create", "purpose": "Scout leads", "model": DEFAULT_AGENT_MODEL}
    async with _running(db_session_factory, teams_api_fake, state=state) as (service, _):
        opened = await _post(service, _dialog("fetch", {"dialog_id": "agent_create"}))
        bad = await _post(service, _dialog("submit", form | {"name": "bad name!"}))
        good = await _post(service, _dialog("submit", form | {"name": "scout"}))

    assert opened["task"]["type"] == "continue", "any member may open the form"
    assert bad["task"]["type"] == "continue" and "Name must be" in json.dumps(bad)
    assert good["task"]["value"].startswith("Created scout")
    [created] = [a for a in state.agents.values() if a["name"] == "scout"]
    assert "Scout leads" in str(created["system"])
    edits = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert edits and edits[-1].url.endswith("/activities/m-7"), "the panel lands on Details"


@pytest.mark.asyncio
async def test_coding_tools_mint_is_admin_only_and_only_the_minter_revokes(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    fetch = {"dialog_id": "agent_coding_tools", "agent": "analyst"}
    admins = (AAD_OBJECT_ID,)
    with structlog.testing.capture_logs() as logs:
        async with _running(db_session_factory, teams_api_fake, admins) as (service, _):
            refused = await _post(service, _dialog("fetch", fetch, user=OTHER_AAD_OBJECT_ID))
            minted = await _post(service, _dialog("fetch", fetch))
            card = json.dumps(minted)
            jti = card.split('"jti": "')[1].split('"')[0]
            revoke = {"action": "agent_coding_tools", "jti": jti}
            other = await _post(service, _dialog("submit", revoke, user=OTHER_AAD_OBJECT_ID))
            mine = await _post(service, _dialog("submit", revoke))
            again = await _post(service, _dialog("submit", revoke))

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

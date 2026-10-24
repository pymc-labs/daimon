"""Execution credentials cannot borrow a concurrent human origin via MCP."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from daimon.adapters.discord.continuation_dispatch import (
    dispatch_pending_continuations as dispatch_discord,
)
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.slack.continuation_dispatch import (
    dispatch_pending_continuations as dispatch_slack,
)
from daimon.adapters.teams.app import TeamsApp
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.continuity.continuation import ContinuationDecision
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.stores.domain import ChatPlatform, Role, TaskContinuationRow, TurnOriginRow
from daimon.core.stores.task_continuations import list_pending_continuations
from daimon.core.stores.turn_origins import create_origin, delete_origin
from daimon.testing import ma_agent
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter
from mux.testing.drivers.anthropic.ma import build_fake_anthropic, list_response
from pydantic import HttpUrl, PostgresDsn, SecretStr
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

SECRET = "origin-session-grant-test-secret!"


@pytest_asyncio.fixture
async def committing_sessionmaker(
    db_engine: AsyncEngine, db_clean: None
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


SOURCE = "agent_source"
TARGET = "agent_destination"


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize("execution", ["routine", "timer", "human"])
@pytest.mark.parametrize("selection", ["empty", "borrowed", "own"])
async def test_registered_handoff_cannot_borrow_human_attendance(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    platform: ChatPlatform,
    execution: str,
    selection: str,
) -> None:
    db = committing_sessionmaker
    now = datetime.now(UTC)
    human_grant, caller_grant = str(uuid.uuid4()), str(uuid.uuid4())
    async with db.begin() as session:
        tenant = await make_tenant(session, platform=platform)
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform=platform, external_id="caller", tenant=tenant, account=account
        )

        async def origin(thread: str, grant: str, attended: bool) -> TurnOriginRow:
            return await create_origin(
                session,
                tenant_id=tenant.id,
                account_id=account.id,
                platform=platform,
                parent_channel_id="parent",
                thread_id=thread,
                responder_ma_agent_id=SOURCE,
                responder_name="source",
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                now=now,
                expires_at=now + timedelta(hours=1),
                attended=attended,
                chat_session_id=grant,
                sender_foreign=False,
                shared_channel=False,
            )

        human = await origin("human-thread", human_grant, True)
        own = (
            None
            if execution == "routine"
            else await origin("4242", caller_grant, execution == "human")
        )
        # A routine also receives a real isolated session credential. Its lack
        # of an origin never permits sole-running-human discovery.
        token, _, _ = await mint_chat_mcp_token(
            session,
            account_id=account.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=SOURCE),
            secret=SECRET.encode(),
            now=now,
            chat_session_id=caller_grant,
        )
    target = ma_agent(id=TARGET, name="daimon", tenant_id=tenant.id)
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([target.model_dump(mode="json")]))
    router.add_agent(target)
    client = build_fake_anthropic(router.dispatch)
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://test/mcp")),
        ),
        sessionmaker=db,
        anthropic=client,
    )
    selected = (
        ""
        if selection == "empty"
        else str(own.id if selection == "own" and own is not None else human.id)
    )
    reply = await call_mcp_tool(
        app,
        token=token,
        name="call_tool",
        arguments={
            "name": "hand_off_task",
            "arguments": {
                "origin_context_id": selected,
                "agent_id": TARGET,
                "continuation": "finish the requested chart",
            },
        },
    )
    allowed = selection == "own" and own is not None
    async with db() as session:
        human_rows = await list_pending_continuations(
            session, tenant_id=tenant.id, platform=platform, thread_id=human.thread_id
        )
        rows = await list_pending_continuations(
            session, tenant_id=tenant.id, platform=platform, thread_id="4242"
        )
    assert human_rows == [], reply
    assert len(rows) == int(allowed), reply
    if not allowed:
        assert "isError" in str(reply) or "origin" in str(reply), reply
    else:
        assert own is not None
        [row] = rows
        assert row.attended is (execution == "human")
        # The producing turn has ended before the real dispatcher runs.
        async with db.begin() as session:
            await delete_origin(session, origin_id=own.id)
    delivered: list[tuple[bool, str]] = []

    async def run_follow_up(queued: TaskContinuationRow, seed: str) -> None:
        delivered.append((queued.attended, seed))

    if platform == "discord":
        thread = MagicMock(spec=discord.Thread)
        thread.id = 4242

        async def history(**_kwargs):
            for message in ():
                yield message

        thread.history.side_effect = history

        async def may_post() -> bool:
            return True

        async def run_discord(queued: TaskContinuationRow, decision: ContinuationDecision) -> None:
            assert decision.seed_user_message is not None
            await run_follow_up(queued, decision.seed_user_message)

        await dispatch_discord(
            db,
            client,
            tenant_id=tenant.id,
            thread=thread,
            run_follow_up=run_discord,
            may_post=may_post,
        )
    elif platform == "slack":
        web = MagicMock(spec=AsyncWebClient)
        web.conversations_replies = AsyncMock(return_value={"messages": []})
        await dispatch_slack(
            db,
            client,
            web,
            tenant_id=tenant.id,
            channel="parent",
            thread_id="4242",
            active_turn=False,
            run_follow_up=run_follow_up,
        )
    else:
        teams = MagicMock(spec=TeamsApp)
        teams.runtime = SimpleNamespace(sessionmaker=db, anthropic=client)
        teams._last_message_at = {"4242": now - timedelta(seconds=1)}

        async def run_teams(queued: TaskContinuationRow, seed: str, *_args: object) -> None:
            await run_follow_up(queued, seed)

        teams._run_continuation = AsyncMock(side_effect=run_teams)
        await TeamsApp._dispatch_continuations(teams, "4242", tenant.id, None)
    assert len(delivered) == int(allowed)
    if allowed:
        assert delivered[0][0] is (execution == "human")

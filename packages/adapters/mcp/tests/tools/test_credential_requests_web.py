"""Credential requests raised in a web chat turn: a notice to the web process, no post."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.credential_requests as credential_requests
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.credential_requests import get_credential_request_by_idempotency_key
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.stores.turn_origins import create_origin
from daimon.core.stores.web_notices import WEB_NOTICE_CHANNEL, parse_web_notice
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from mux.testing.drivers.anthropic.ma_models import ma_model_config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_request_agent_key = credential_requests._request_agent_key_impl  # pyright: ignore[reportPrivateUsage]

_THREAD = f"web:{uuid.uuid4()}"


def _client(tenant_id: uuid.UUID) -> AsyncAnthropic:
    agent = ma_agent(
        id="ag_web",
        name="daimon",
        model=ma_model_config("claude-sonnet-4-6", speed="standard"),
        metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: "daimon"},
    ).model_dump(mode="json")
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([agent]))
    return build_fake_anthropic(router.dispatch)


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], client: Any) -> McpRuntime:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=client,
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


async def _web_origin(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession, platform: str
) -> tuple[AuthIdentity, TurnOriginRow]:
    tenant = await make_tenant(db_session)
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant.id,
        role=Role.USER,
        platform=platform,
        external_id=None,
        platform_user_id="person-1",
        is_admin=False,
    )
    await make_account(db_session, tenant=tenant, id=auth.account_id)
    await db_session.commit()
    now = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=auth.account_id,
            platform="web",
            parent_channel_id="web",
            thread_id=_THREAD,
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=auth.role,
            sender_foreign=False,
            shared_channel=False,
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
    return auth, origin


@asynccontextmanager
async def _listening(engine: AsyncEngine) -> AsyncIterator[list[str]]:
    heard: list[str] = []
    async with engine.connect() as conn:
        raw = (await conn.get_raw_connection()).driver_connection
        assert raw is not None, "the asyncpg connection must be reachable"

        def on_notice(_conn: object, _pid: int, _channel: str, payload: str) -> None:
            heard.append(payload)

        await raw.add_listener(WEB_NOTICE_CHANNEL, on_notice)
        try:
            yield heard
        finally:
            await raw.remove_listener(WEB_NOTICE_CHANNEL, on_notice)


async def _heard(heard: list[str]) -> None:
    for _ in range(50):
        if heard:
            return
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("platform", ["discord", "web"])
async def test_a_web_turn_key_request_notifies_the_web_process_without_the_token(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_nullpool_engine: AsyncEngine,
    platform: str,
) -> None:
    auth, origin = await _web_origin(committing_sessionmaker, db_session, platform)
    runtime = _runtime(committing_sessionmaker, _client(auth.tenant_id))

    async with _listening(db_nullpool_engine) as heard:
        result = await _request_agent_key(
            runtime,
            auth,
            agent_name="daimon",
            key="TOGGL_API_TOKEN",
            purpose="tracking time",
            channel_id="ignored",
            pending_task="log my hours for today",
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_web",
        )
        await _heard(heard)

    assert len(heard) == 1, "one notice reaches the web process"
    notice = parse_web_notice(heard[0])
    assert notice is not None, "the payload is a notice this version reads"
    assert notice.thread_id == _THREAD, "the notice names the web chat"
    assert str(notice.request_id) == result.message_id, "the card id is the request's id"
    async with committing_sessionmaker() as session:
        row = await get_credential_request_by_idempotency_key(
            session, idempotency_key=notice.request_id
        )
    assert row is not None, "the notice names a stored request"
    assert row.platform == "web", "the request belongs to the web chat"
    assert row.channel_id == _THREAD, "the card lands in the turn's own chat"
    assert row.requester_platform_user_id == "person-1", "only the requester may save it"
    assert row.posted_message_id == result.message_id, "the card id is stored"
    assert row.token not in heard[0], "the token never leaves its row"
    assert set(json.loads(heard[0])) == {"kind", "thread_id", "request_id"}, "nothing else"


async def test_a_web_turn_refuses_a_key_file(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    auth, origin = await _web_origin(committing_sessionmaker, db_session, "web")
    runtime = _runtime(committing_sessionmaker, _client(auth.tenant_id))

    with pytest.raises(ToolError, match="one named key"):
        await _request_agent_key(
            runtime,
            auth,
            agent_name="daimon",
            key=None,
            purpose="loading several keys",
            channel_id="ignored",
            origin_context_id=str(origin.id),
            expected_ma_agent_id="ag_web",
        )
    count = await db_session.scalar(text("SELECT count(*) FROM credential_requests"))
    assert count == 0, "no request is minted for a form the web app cannot show"


async def test_only_the_key_request_is_offered_in_web_turns() -> None:
    mcp = FastMCP("t")
    credential_requests.register_credential_request_tools(mcp, MagicMock())
    tags = {tool.name: tool.tags for tool in await mcp.list_tools()}
    assert "web" in tags["request_agent_key"], "web turns can ask for a key"
    assert "web" not in tags["request_mcp_token"], "connections stay on chat apps"

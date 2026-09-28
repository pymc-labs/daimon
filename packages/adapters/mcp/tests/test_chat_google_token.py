"""Chat provisioning -> signed vault JWT -> real MCP auth -> mocked Google."""

from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CredentialsSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.sessions import create_session
from daimon.core.stores.agent_google_binding import upsert_agent_google_binding
from daimon.testing.asgi import mcp_session
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic
from daimon.testing.ma_models import ma_agent, ma_environment, ma_session
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("binding", ["own", "other_agent", "other_tenant"])
async def test_chat_google_token_is_scoped_to_executing_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
) -> None:
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        other_tenant = await make_tenant(session)
        agent = ma_agent()
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=agent.id)
        bound_agent_id = {
            "own": agent_id,
            "other_agent": derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_other"),
            "other_tenant": derive_agent_uuid(tenant_id=other_tenant.id, ma_agent_id=agent.id),
        }[binding]
        await upsert_agent_google_binding(
            session, agent_id=bound_agent_id, email="bound@example.com", scopes=["scope.read"]
        )

    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/vaults":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if request.method == "POST" and path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "id": "vlt_chat",
                    "type": "vault",
                    "created_at": "2026-04-24T00:00:00Z",
                    "display_name": json.loads(request.content)["display_name"],
                },
            )
        if request.method == "POST" and path == "/v1/vaults/vlt_chat/credentials":
            body = json.loads(request.content)
            captured.append(body["auth"]["token"])
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_chat",
                    "type": "vault_credential",
                    "vault_id": "vlt_chat",
                    "auth": {"type": "static_bearer", "mcp_server_url": "https://x/mcp"},
                },
            )
        if request.method == "POST" and path == "/v1/sessions":
            assert json.loads(request.content)["vault_ids"] == ["vlt_chat"]
            return httpx.Response(200, json=ma_session().model_dump(mode="json"))
        raise AssertionError(f"unexpected MA request: {request.method} {path}")

    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        credentials=CredentialsSettings(google_sa_json=SecretStr('{"type":"service_account"}')),
    )
    anthropic = build_fake_anthropic(handler)
    await create_session(
        anthropic,
        agent=agent,
        environment=ma_environment(),
        mcp_settings=settings.mcp,
        account_id=account.id,
        agent_uuid=agent_id,
        session_factory=sessionmaker,
    )
    google_creds = Mock(token="mock-google-access-token")
    google_factory = Mock(return_value=google_creds)
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_info", google_factory
    )
    app = create_mcp_app(settings=settings, sessionmaker=sessionmaker, anthropic=anthropic)
    result = await mcp_session(
        app,
        token=captured[0],
        method="tools/call",
        params={
            "name": "call_tool",
            "arguments": {"name": "get_cli_token", "arguments": {"service": "gcloud"}},
        },
    )
    payload = json.dumps(result)
    if binding == "own":
        assert "mock-google-access-token" in payload
        google_factory.assert_called_once_with(
            {"type": "service_account"}, scopes=["scope.read"], subject="bound@example.com"
        )
        google_creds.refresh.assert_called_once()
    else:
        assert "no Google identity bound" in payload
        assert "bind-google" in payload
        assert "mock-google-access-token" not in payload
        google_factory.assert_not_called()

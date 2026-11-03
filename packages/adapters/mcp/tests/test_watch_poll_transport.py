"""The private executor accepts lease identifiers, never caller-selected source or identity."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.mcp.watch_executor import create_poll_app


@pytest.fixture(autouse=True)
def executor_secret(monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__EXECUTOR_SECRET", "s" * 32)


def private_app(runtime=None):
    return create_poll_app(runtime or SimpleNamespace(session_factory=MagicMock()))


@pytest.mark.parametrize("enabled", [True, False])
async def test_executor_ignores_forged_body_and_respects_producer_gate(monkeypatch, enabled):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", str(enabled).lower())
    run = AsyncMock(return_value=True)
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.run_poll", run)
    runtime = SimpleNamespace(session_factory=MagicMock())
    watch_id, lease = uuid.uuid4(), str(uuid.uuid4())
    monkeypatch.setattr(
        "daimon.core.stores.watches.get_watch",
        AsyncMock(
            return_value=SimpleNamespace(
                lease_owner=lease, lease_expires_at=datetime.now(UTC) + timedelta(minutes=1)
            )
        ),
    )
    app = private_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://os"
    ) as client:
        response = await client.post(
            f"/internal/watches/{watch_id}/poll",
            headers={"X-Watch-Lease": lease, "X-Watch-Executor-Key": "s" * 32},
            json={
                "owner": "evil",
                "tool": "http_get",
                "arguments": {"url": "https://outside"},
                "instructions": "write",
                "payload": "forged",
            },
        )
    assert response.status_code == (204 if enabled else 503)
    if enabled:
        run.assert_awaited_once_with(runtime, watch_id, lease)
    else:
        run.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-Watch-Lease", "invalid")],
        [("X-Watch-Lease", str(uuid.uuid4())), ("X-Watch-Lease", str(uuid.uuid4()))],
    ],
)
async def test_executor_refuses_missing_malformed_or_duplicate_leases(monkeypatch, headers):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    run = AsyncMock()
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.run_poll", run)
    app = private_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://os"
    ) as client:
        response = await client.post(
            f"/internal/watches/{uuid.uuid4()}/poll",
            headers=list(headers) + [("X-Watch-Executor-Key", "s" * 32)],
        )
    assert response.status_code == 403
    run.assert_not_called()


async def test_junk_requests_cannot_starve_real_polls(monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    run = AsyncMock(return_value=True)
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.run_poll", run)
    watch_id, lease = uuid.uuid4(), str(uuid.uuid4())
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr("daimon.core.stores.watches.get_watch", lookup)
    app = private_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://private"
    ) as client:
        for _ in range(125):
            response = await client.post(
                f"/internal/watches/{uuid.uuid4()}/poll",
                headers={"X-Watch-Lease": str(uuid.uuid4())},
            )
            assert response.status_code == 403
        lookup.assert_not_called()
        for _ in range(125):
            response = await client.post(
                f"/internal/watches/{uuid.uuid4()}/poll",
                headers={"X-Watch-Lease": str(uuid.uuid4()), "X-Watch-Executor-Key": "s" * 32},
            )
            assert response.status_code == 403
        lookup.return_value = SimpleNamespace(
            lease_owner=lease, lease_expires_at=datetime.now(UTC) + timedelta(minutes=1)
        )
        response = await client.post(
            f"/internal/watches/{watch_id}/poll",
            headers={"X-Watch-Lease": lease, "X-Watch-Executor-Key": "s" * 32},
        )
        assert response.status_code == 204
    run.assert_awaited_once()


async def test_poll_executor_is_absent_from_public_mcp_app(monkeypatch, sessionmaker):
    from daimon.adapters.mcp.server import create_mcp_app
    from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from mux.testing.drivers.anthropic.ma import build_stub_anthropic
    from pydantic import HttpUrl, PostgresDsn, SecretStr

    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    run = AsyncMock(return_value=True)
    monkeypatch.setattr("daimon.adapters.mcp.watch_polling.run_poll", run)
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            mcp=McpSettings(
                jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://os.example/mcp")
            ),
        ),
        sessionmaker=sessionmaker,
        anthropic=build_stub_anthropic(lambda _: httpx.Response(404)),
        auth=StaticTokenVerifier(tokens={}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://os"
    ) as client:
        result = await client.post(
            f"/internal/watches/{uuid.uuid4()}/poll", headers={"X-Watch-Lease": str(uuid.uuid4())}
        )
    assert result.status_code == 404
    run.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-Watch-Executor-Key", "wrong")],
        [("X-Watch-Executor-Key", "s" * 32), ("X-Watch-Executor-Key", "s" * 32)],
    ],
)
async def test_executor_authentication_precedes_database_work(monkeypatch, headers):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    runtime = SimpleNamespace(session_factory=MagicMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(private_app(runtime)), base_url="http://private"
    ) as client:
        result = await client.post(
            f"/internal/watches/{uuid.uuid4()}/poll",
            headers=headers + [("X-Watch-Lease", str(uuid.uuid4()))],
        )
    assert result.status_code == 403
    runtime.session_factory.assert_not_called()

"""Tests for the report publishing MCP tools: the impl error mapping, the cap's
Decimal boundary, and the reachability property that makes these two tools
invisible to a report's own agent-scoped token.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import create_autospec

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.tools.publish import (
    _delete_report_impl,  # pyright: ignore[reportPrivateUsage]
    _publish_report_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    ReportHostSettings,
    Settings,
)
from daimon.core.errors import DaimonError
from daimon.core.mcp_auth import mint_jwt
from daimon.core.reports.host_client import Recipient, ReportHostError
from daimon.core.reports.publish import (
    DeleteResult,
    HostNotConfiguredError,
    InvalidSlugError,
    PublishResult,
)
from daimon.core.scope import DeploymentDefault
from daimon.testing.factories import make_account, make_tenant
from factories import make_jwt
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

from ..factories import mcp_session

pytestmark = pytest.mark.asyncio

SECRET = "a" * 32


def _make_settings(
    *,
    host_url: str | None = "http://report-host:8002",
    admin_secret: str | None = "admin-secret",
    jwt_secret: str | None = SECRET,
) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://daimon:daimon@localhost:5432/daimon")
        ),
        anthropic=AnthropicSettings(api_key=SecretStr("test-key")),
        report_host=ReportHostSettings(
            host_url=HttpUrl(host_url) if host_url else None,
            admin_secret=SecretStr(admin_secret) if admin_secret else None,
        ),
        mcp=McpSettings(jwt_secret=SecretStr(jwt_secret) if jwt_secret else None),
        _env_file=None,  # type: ignore[call-arg]
    )


def _make_runtime(settings: Settings) -> McpRuntime:
    fake_sessionmaker: async_sessionmaker[AsyncSession] = create_autospec(
        async_sessionmaker, instance=True
    )
    return McpRuntime(
        session_factory=fake_sessionmaker,
        client=AsyncAnthropic(api_key="test-key"),
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


# ---------------------------------------------------------------------------
# _publish_report_impl / _delete_report_impl — error mapping, faking the core
# orchestration at the boundary of the impl (it's exhaustively tested at the
# host-client/publish-orchestration layer already, in plan 21-22).
# ---------------------------------------------------------------------------


async def test_publish_report_impl_raises_when_jwt_secret_unset() -> None:
    runtime = _make_runtime(_make_settings(jwt_secret=None))
    with pytest.raises(ToolError, match="not configured"):
        await _publish_report_impl(
            runtime,
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            slug="q1-results",
            title="Q1 results",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="2.50",
            agent=None,
        )


async def test_publish_report_impl_maps_host_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(**_kwargs: object) -> PublishResult:
        raise HostNotConfiguredError("report host not configured: host_url is unset")

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", fake)
    runtime = _make_runtime(_make_settings())
    with pytest.raises(ToolError, match="report host not configured") as exc_info:
        await _publish_report_impl(
            runtime,
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            slug="q1-results",
            title="Q1 results",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="2.50",
            agent=None,
        )
    assert isinstance(exc_info.value.__cause__, HostNotConfiguredError), (
        "the domain error must be preserved as the tool error's cause"
    )


async def test_publish_report_impl_maps_invalid_slug_to_domain_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(**_kwargs: object) -> PublishResult:
        raise InvalidSlugError("invalid report slug: 'Q1 Results'")

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", fake)
    runtime = _make_runtime(_make_settings())
    with pytest.raises(ToolError, match="invalid report slug: 'Q1 Results'") as exc_info:
        await _publish_report_impl(
            runtime,
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            slug="Q1 Results",
            title="Q1 results",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="2.50",
            agent=None,
        )
    assert isinstance(exc_info.value.__cause__, InvalidSlugError)


async def test_publish_report_impl_maps_unknown_agent_to_domain_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(**_kwargs: object) -> PublishResult:
        raise DaimonError("agent 'ghost' not found")

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", fake)
    runtime = _make_runtime(_make_settings())
    with pytest.raises(ToolError, match="agent 'ghost' not found") as exc_info:
        await _publish_report_impl(
            runtime,
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            slug="q1-results",
            title="Q1 results",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="2.50",
            agent="ghost",
        )
    assert isinstance(exc_info.value.__cause__, DaimonError)


async def test_publish_report_impl_maps_host_error_carrying_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(**_kwargs: object) -> PublishResult:
        raise ReportHostError("report host returned 500: internal error")

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", fake)
    runtime = _make_runtime(_make_settings())
    with pytest.raises(ToolError, match="report host returned 500: internal error") as exc_info:
        await _publish_report_impl(
            runtime,
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            slug="q1-results",
            title="Q1 results",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="2.50",
            agent=None,
        )
    assert isinstance(exc_info.value.__cause__, ReportHostError)


async def test_publish_report_impl_maps_timeout_distinctly(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**_kwargs: object) -> PublishResult:
        raise ReportHostError("report host request failed: timed out") from httpx.TimeoutException(
            "timed out"
        )

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", fake)
    runtime = _make_runtime(_make_settings())
    with pytest.raises(ToolError, match="report host timed out"):
        await _publish_report_impl(
            runtime,
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            slug="q1-results",
            title="Q1 results",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="2.50",
            agent=None,
        )


async def test_publish_report_impl_maps_transport_failure_distinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(**_kwargs: object) -> PublishResult:
        raise ReportHostError(
            "report host request failed: connection refused"
        ) from httpx.TransportError("connection refused")

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", fake)
    runtime = _make_runtime(_make_settings())
    with pytest.raises(ToolError, match="report host unreachable: connection refused"):
        await _publish_report_impl(
            runtime,
            tenant_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            slug="q1-results",
            title="Q1 results",
            recipients=[Recipient(name="Ada", label="ada")],
            cap_usd="2.50",
            agent=None,
        )


async def test_publish_report_impl_happy_path_returns_upload_url_and_links_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(**_kwargs: object) -> PublishResult:
        return PublishResult(
            upload_url="http://report-host:8002/publish/tok",
            links={"Ada": "http://report-host:8002/r/q1-results?t=abc"},
        )

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", fake)
    runtime = _make_runtime(_make_settings())
    out = await _publish_report_impl(
        runtime,
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        slug="q1-results",
        title="Q1 results",
        recipients=[Recipient(name="Ada", label="ada")],
        cap_usd="2.50",
        agent=None,
    )
    assert out == {
        "upload_url": "http://report-host:8002/publish/tok",
        "links": {"Ada": "http://report-host:8002/r/q1-results?t=abc"},
    }, "the happy path must return the core result unchanged"


@pytest.mark.parametrize("raw_cap", ["2.50", 2.5])
async def test_publish_report_impl_cap_reaches_core_as_exact_decimal(
    monkeypatch: pytest.MonkeyPatch, raw_cap: str | float
) -> None:
    """A string ("2.50") and a number (2.5, the JSON shape of the same value)
    both reach the core function as a Decimal, never a bare float."""
    captured: dict[str, object] = {}

    async def fake(**kwargs: object) -> PublishResult:
        captured.update(kwargs)
        return PublishResult(upload_url="http://h/publish/tok", links={})

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.publish_report", fake)
    runtime = _make_runtime(_make_settings())
    await _publish_report_impl(
        runtime,
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        slug="q1-results",
        title="Q1 results",
        recipients=[Recipient(name="Ada", label="ada")],
        cap_usd=raw_cap,
        agent=None,
    )
    cap = captured["cap_usd"]
    assert isinstance(cap, Decimal), f"cap_usd must reach core as a Decimal, got {type(cap)}"
    assert cap == Decimal("2.50"), f"expected Decimal('2.50'), got {cap!r}"


async def test_delete_report_impl_raises_host_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(**_kwargs: object) -> DeleteResult:
        raise HostNotConfiguredError("report host not configured: host_url is unset")

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.delete_report", fake)
    runtime = _make_runtime(_make_settings())
    with pytest.raises(ToolError, match="report host not configured") as exc_info:
        await _delete_report_impl(runtime, tenant_id=uuid.uuid4(), slug="q1-results")
    assert isinstance(exc_info.value.__cause__, HostNotConfiguredError)


async def test_delete_report_impl_maps_host_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(**_kwargs: object) -> DeleteResult:
        raise ReportHostError("report host returned 404: not found")

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.delete_report", fake)
    runtime = _make_runtime(_make_settings())
    with pytest.raises(ToolError, match="report host returned 404: not found") as exc_info:
        await _delete_report_impl(runtime, tenant_id=uuid.uuid4(), slug="q1-results")
    assert isinstance(exc_info.value.__cause__, ReportHostError)


async def test_delete_report_impl_passes_caller_tenant_and_returns_core_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake(**kwargs: object) -> DeleteResult:
        captured.update(kwargs)
        return DeleteResult(tokens_revoked=2, host_removed=True)

    monkeypatch.setattr("daimon.adapters.mcp.tools.publish.delete_report", fake)
    runtime = _make_runtime(_make_settings())
    tenant_id = uuid.uuid4()
    out = await _delete_report_impl(runtime, tenant_id=tenant_id, slug="q1-results")

    assert captured["tenant_id"] == tenant_id, "the caller's own tenant must be passed through"
    assert captured["slug"] == "q1-results"
    assert out == {"tokens_revoked": 2, "host_removed": True}, (
        "delete_report must return what the core reported, unchanged"
    )


# ---------------------------------------------------------------------------
# Reachability — the point of this file. A report's own token carries an
# agent_id claim; IdentityMiddleware narrows that session to exactly the
# agent-chat-tagged tools. publish_report/delete_report are untagged, so
# they must never appear for such a session, while an ordinary operator
# token (no agent_id claim) must see both.
# ---------------------------------------------------------------------------


def _make_app(sessionmaker: async_sessionmaker[AsyncSession]) -> ASGIApp:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
    )


@pytest.mark.parametrize("tool_name", ["publish_report", "delete_report"])
async def test_operator_token_discovers_publish_and_delete_report_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
) -> None:
    """An ordinary platform-user token (no agent_id claim) discovers both
    tools via search_tools — the meta-tool discovery surface every
    non-narrowed session uses (tools/list only returns the meta-tools
    themselves for such a session, per the BM25 collapse). This is the
    positive control that keeps the reachability test below from passing
    vacuously by the tools simply not existing."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="publish-reachability")
        account = await make_account(s, tenant=tenant)
    token = make_jwt(account_id=account.id)
    app = _make_app(sessionmaker)

    result = await mcp_session(
        app,
        token=token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": tool_name.replace("_", " ")}},
    )
    call_result = result.get("result", result)
    content = call_result.get("content", [])  # type: ignore[union-attr]
    output_text = " ".join(item.get("text", "") for item in content if isinstance(item, dict))
    assert tool_name in output_text, (
        f"{tool_name} must be discoverable by an operator token; got: {output_text!r}"
    )


async def test_agent_scoped_token_does_not_discover_publish_or_delete_report_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A report's own token carries an agent_id claim, which narrows the
    session to exactly the agent-chat-tagged tool set. Neither publish tool
    is a member of that set, so neither may appear here — the property that
    stops a reader session from ever publishing or deleting a report."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="publish-reachability-agent")
        account = await make_account(s, tenant=tenant)
    token = mint_jwt(
        account_id=account.id, secret=SECRET.encode(), now=datetime.now(UTC), agent_id=uuid.uuid4()
    )
    app = _make_app(sessionmaker)

    result = await mcp_session(app, token=token, method="tools/list")
    payload = result.get("result", result)
    tool_names = {t["name"] for t in payload.get("tools", [])}  # type: ignore[union-attr]

    assert not ({"publish_report", "delete_report"} & tool_names), (
        f"an agent-scoped token must never discover either publish tool; got: {sorted(tool_names)}"
    )
    assert "ask" in tool_names, (
        "the agent-chat tool set must still be visible — otherwise this test "
        "would pass vacuously by narrowing to nothing at all"
    )

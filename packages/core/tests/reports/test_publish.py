"""Tests for daimon.core.reports.publish.

Transport-level MA fakes (MARouter/build_fake_anthropic), a mock HTTP
transport for the report host, and a real Postgres session (via
db_session_factory) for the token rows — per guideline:testing.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.config import ReportHostSettings
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_SPEC_HASH,
    MA_METADATA_KEY_TENANT,
    tenant_scoped_display_title,
)
from daimon.core.errors import DaimonError
from daimon.core.reader_agent import READER_SKILL_NAME
from daimon.core.reports.host_client import Recipient, ReportHostError
from daimon.core.reports.publish import (
    DeleteResult,
    HostNotConfiguredError,
    InvalidSlugError,
    delete_report,
    publish_report,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.mcp_tokens import get_mcp_token, list_live_tokens_by_label
from daimon.testing.factories import make_account, make_mcp_token, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
_JWT_SECRET = b"jwt-shared-secret-well-over-the-32-byte-minimum"
_MAX_BUNDLE_BYTES = 25 * 1024 * 1024


def _settings() -> ReportHostSettings:
    return ReportHostSettings(
        host_url=HttpUrl("http://report-host:8002"), admin_secret=SecretStr("shared-secret")
    )


def _host_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _source_agent_dict(
    *, id_: str, name: str, tenant_id: uuid.UUID, spec_hash: str = "src-hash-1", version: int = 1
) -> dict[str, Any]:
    metadata: dict[str, str] = {
        MA_METADATA_KEY_TENANT: str(tenant_id),
        MA_METADATA_KEY_NAME: name,
        MA_METADATA_KEY_SPEC_HASH: spec_hash,
    }
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": id_,
            "type": "agent",
            "name": name,
            "model": {"id": "claude-sonnet-4-6"},
            "metadata": metadata,
            "description": None,
            "archived_at": None,
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
            "version": version,
            "mcp_servers": [],
            "skills": [],
            "tools": [],
            "system": "You are alpha, a data analyst.",
        }
    ).model_dump(mode="json")


def _ma_router(agents: list[dict[str, Any]], *, tenant_id: uuid.UUID) -> MARouter:
    """List + per-id retrieve + a skills list resolving the report-reader ref."""
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response(agents))

    def _retrieve(_req: httpx.Request, match: re.Match[str]) -> httpx.Response:
        agent_id = match.group(1)
        for agent in agents:
            if agent["id"] == agent_id:
                return httpx.Response(200, json=agent)
        return httpx.Response(
            404,
            json={"type": "error", "error": {"type": "not_found_error", "message": "not found"}},
        )

    router.add("GET", r"/v1/agents/([^/]+)", _retrieve)
    canonical_title = tenant_scoped_display_title(tenant_id=tenant_id, name=READER_SKILL_NAME)
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [
                {
                    "id": "sk_reader_resolved",
                    "type": "custom",
                    "display_title": canonical_title,
                    "latest_version": "1",
                    "created_at": "2026-06-01T00:00:00Z",
                    "updated_at": "2026-06-01T00:00:00Z",
                    "source": "custom",
                }
            ]
        ),
    )
    return router


def _add_create_route(router: MARouter, captured: dict[str, Any], *, reader_id: str) -> None:
    def on_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        captured.update(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "id": reader_id,
                "type": "agent",
                "name": captured["name"],
                "model": {"id": "claude-sonnet-4-6"},
                "metadata": captured["metadata"],
                "description": None,
                "archived_at": None,
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
                "version": 1,
                "mcp_servers": [],
                "skills": [{"type": "custom", "skill_id": "sk_reader_resolved", "version": "1"}],
                "tools": [],
                "system": captured.get("system", ""),
            },
        )

    router.add("POST", r"/v1/agents", on_create)


def _reader_agent_dict(
    *, id_: str, name: str, tenant_id: uuid.UUID, metadata: dict[str, str], version: int = 1
) -> dict[str, Any]:
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": id_,
            "type": "agent",
            "name": name,
            "model": {"id": "claude-sonnet-4-6"},
            "metadata": {
                MA_METADATA_KEY_TENANT: str(tenant_id),
                MA_METADATA_KEY_NAME: name,
                **metadata,
            },
            "description": None,
            "archived_at": None,
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
            "version": version,
            "mcp_servers": [],
            "skills": [{"type": "custom", "skill_id": "sk_reader_resolved", "version": "1"}],
            "tools": [],
            "system": "",
        }
    ).model_dump(mode="json")


async def _seed_tenant(
    session_factory: async_sessionmaker[AsyncSession], *, workspace_id: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        tenant = await make_tenant(session, workspace_id=workspace_id)
        account = await make_account(session, tenant=tenant)
        await session.commit()
    return tenant.id, account.id


# ---------------------------------------------------------------------------
# publish_report
# ---------------------------------------------------------------------------


async def test_publish_report_registers_with_host_and_returns_upload_url_and_links(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed_tenant(db_session_factory)
    source = _source_agent_dict(id_="ag_src", name="alpha", tenant_id=tenant_id)
    ma_router = _ma_router([source], tenant_id=tenant_id)
    created: dict[str, Any] = {}
    _add_create_route(ma_router, created, reader_id="ag_reader")
    anthropic = build_fake_anthropic(ma_router.dispatch)

    host_calls: list[httpx.Request] = []

    def host_handler(req: httpx.Request) -> httpx.Response:
        host_calls.append(req)
        return httpx.Response(
            200, json={"slug": "q3", "links": [{"name": "Ada", "link": "https://r.example/ada"}]}
        )

    result = await publish_report(
        anthropic=anthropic,
        session_factory=db_session_factory,
        http_client=_host_client(host_handler),
        report_host_settings=_settings(),
        jwt_secret=_JWT_SECRET,
        max_bundle_bytes=_MAX_BUNDLE_BYTES,
        default=DeploymentDefault(),
        tenant_id=tenant_id,
        account_id=account_id,
        slug="q3",
        title="Q3 financials",
        recipients=[Recipient(name="Ada", label="ada-1")],
        cap_usd=Decimal("2.00"),
        agent="alpha",
        now=NOW,
    )

    assert len(host_calls) == 1, "exactly one host registration must be issued"
    body = json.loads(host_calls[0].content)
    assert body["agent_name"] == "alpha"
    assert body["seam_token"], "the freshly minted token must be in the request body"
    assert body["cap_usd"] == "2.00"
    assert result.links == {"Ada": "https://r.example/ada"}
    assert result.upload_url.startswith("http://report-host:8002/publish/")


async def test_publish_report_leaves_a_live_token_row_findable_by_label(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed_tenant(db_session_factory)
    source = _source_agent_dict(id_="ag_src", name="alpha", tenant_id=tenant_id)
    ma_router = _ma_router([source], tenant_id=tenant_id)
    _add_create_route(ma_router, {}, reader_id="ag_reader")
    anthropic = build_fake_anthropic(ma_router.dispatch)

    def host_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"slug": "q3", "links": []})

    await publish_report(
        anthropic=anthropic,
        session_factory=db_session_factory,
        http_client=_host_client(host_handler),
        report_host_settings=_settings(),
        jwt_secret=_JWT_SECRET,
        max_bundle_bytes=_MAX_BUNDLE_BYTES,
        default=DeploymentDefault(),
        tenant_id=tenant_id,
        account_id=account_id,
        slug="q3",
        title="Q3 financials",
        recipients=[],
        cap_usd=Decimal("2.00"),
        agent="alpha",
        now=NOW,
    )

    async with db_session_factory() as session:
        rows = await list_live_tokens_by_label(session, tenant_id=tenant_id, label="report:q3")
    assert len(rows) == 1, (
        "delete_report finds a report's token by this exact label — it must exist"
    )
    assert rows[0].account_id == account_id, (
        "the token must be minted under the publisher's account"
    )


async def test_publish_report_omitting_agent_resolves_the_tenants_configured_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed_tenant(db_session_factory)
    source = _source_agent_dict(id_="ag_src", name="alpha", tenant_id=tenant_id)
    ma_router = _ma_router([source], tenant_id=tenant_id)
    _add_create_route(ma_router, {}, reader_id="ag_reader")
    anthropic = build_fake_anthropic(ma_router.dispatch)

    host_calls: list[httpx.Request] = []

    def host_handler(req: httpx.Request) -> httpx.Response:
        host_calls.append(req)
        return httpx.Response(200, json={"slug": "q3", "links": []})

    await publish_report(
        anthropic=anthropic,
        session_factory=db_session_factory,
        http_client=_host_client(host_handler),
        report_host_settings=_settings(),
        jwt_secret=_JWT_SECRET,
        max_bundle_bytes=_MAX_BUNDLE_BYTES,
        default=DeploymentDefault(agent_name="alpha"),
        tenant_id=tenant_id,
        account_id=account_id,
        slug="q3",
        title="Q3 financials",
        recipients=[],
        cap_usd=Decimal("2.00"),
        agent=None,
        now=NOW,
    )

    body = json.loads(host_calls[0].content)
    assert body["agent_name"] == "alpha", (
        "an omitted agent must resolve through the deployment-default cascade"
    )


async def test_publish_report_unknown_agent_raises_before_any_token_or_host_call(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed_tenant(db_session_factory)
    source = _source_agent_dict(id_="ag_src", name="alpha", tenant_id=tenant_id)
    ma_router = _ma_router([source], tenant_id=tenant_id)
    anthropic = build_fake_anthropic(ma_router.dispatch)

    host_calls: list[httpx.Request] = []

    def host_handler(req: httpx.Request) -> httpx.Response:
        host_calls.append(req)
        return httpx.Response(200, json={"slug": "q3", "links": []})

    with pytest.raises(DaimonError):
        await publish_report(
            anthropic=anthropic,
            session_factory=db_session_factory,
            http_client=_host_client(host_handler),
            report_host_settings=_settings(),
            jwt_secret=_JWT_SECRET,
            max_bundle_bytes=_MAX_BUNDLE_BYTES,
            default=DeploymentDefault(),
            tenant_id=tenant_id,
            account_id=account_id,
            slug="q3",
            title="Q3",
            recipients=[],
            cap_usd=Decimal("2.00"),
            agent="ghost",
            now=NOW,
        )

    assert host_calls == [], "an unknown source agent must never reach the host"
    async with db_session_factory() as session:
        rows = await list_live_tokens_by_label(session, tenant_id=tenant_id, label="report:q3")
    assert rows == [], "an unknown source agent must never mint a token"


async def test_publish_report_invalid_slug_raises_before_any_ma_call(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed_tenant(db_session_factory)
    call_count = 0

    def counting_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"data": [], "next_page": None})

    anthropic = build_fake_anthropic(counting_handler)

    with pytest.raises(InvalidSlugError):
        await publish_report(
            anthropic=anthropic,
            session_factory=db_session_factory,
            http_client=_host_client(lambda _req: httpx.Response(200, json={})),
            report_host_settings=_settings(),
            jwt_secret=_JWT_SECRET,
            max_bundle_bytes=_MAX_BUNDLE_BYTES,
            default=DeploymentDefault(),
            tenant_id=tenant_id,
            account_id=account_id,
            slug="Not A Valid Slug!",
            title="t",
            recipients=[],
            cap_usd=Decimal("2.00"),
            agent="alpha",
            now=NOW,
        )

    assert call_count == 0, "an invalid slug must fail before any MA call is issued"


async def test_publish_report_unconfigured_host_settings_raises_before_any_ma_call(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed_tenant(db_session_factory)
    call_count = 0

    def counting_handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"data": [], "next_page": None})

    anthropic = build_fake_anthropic(counting_handler)

    with pytest.raises(HostNotConfiguredError):
        await publish_report(
            anthropic=anthropic,
            session_factory=db_session_factory,
            http_client=_host_client(lambda _req: httpx.Response(200, json={})),
            report_host_settings=ReportHostSettings(host_url=None, admin_secret=None),
            jwt_secret=_JWT_SECRET,
            max_bundle_bytes=_MAX_BUNDLE_BYTES,
            default=DeploymentDefault(),
            tenant_id=tenant_id,
            account_id=account_id,
            slug="q3",
            title="t",
            recipients=[],
            cap_usd=Decimal("2.00"),
            agent="alpha",
            now=NOW,
        )

    assert call_count == 0, "unconfigured report-host settings must fail before any MA call"


async def test_publish_report_failed_host_registration_leaves_zero_live_tokens(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The load-bearing unwind test: a failed PUT must revoke the token it just minted."""
    tenant_id, account_id = await _seed_tenant(db_session_factory)
    source = _source_agent_dict(id_="ag_src", name="alpha", tenant_id=tenant_id)
    ma_router = _ma_router([source], tenant_id=tenant_id)
    _add_create_route(ma_router, {}, reader_id="ag_reader")
    anthropic = build_fake_anthropic(ma_router.dispatch)

    def failing_host_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(ReportHostError):
        await publish_report(
            anthropic=anthropic,
            session_factory=db_session_factory,
            http_client=_host_client(failing_host_handler),
            report_host_settings=_settings(),
            jwt_secret=_JWT_SECRET,
            max_bundle_bytes=_MAX_BUNDLE_BYTES,
            default=DeploymentDefault(),
            tenant_id=tenant_id,
            account_id=account_id,
            slug="q3",
            title="t",
            recipients=[],
            cap_usd=Decimal("2.00"),
            agent="alpha",
            now=NOW,
        )

    async with db_session_factory() as session:
        rows = await list_live_tokens_by_label(session, tenant_id=tenant_id, label="report:q3")
    assert rows == [], "a failed host registration must leave zero live tokens for this report"


async def test_republish_reuses_reader_variant_and_mints_a_second_token(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-publishing the same slug reuses the derived variant (no second agents.create)
    and mints a second token — this implementation does not revoke the first
    (only a *failed* registration triggers revocation)."""
    tenant_id, account_id = await _seed_tenant(db_session_factory)
    source = _source_agent_dict(id_="ag_src", name="alpha", tenant_id=tenant_id)
    ma_router = _ma_router([source], tenant_id=tenant_id)
    created: dict[str, Any] = {}
    _add_create_route(ma_router, created, reader_id="ag_reader")
    anthropic = build_fake_anthropic(ma_router.dispatch)

    def host_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"slug": "q3", "links": []})

    await publish_report(
        anthropic=anthropic,
        session_factory=db_session_factory,
        http_client=_host_client(host_handler),
        report_host_settings=_settings(),
        jwt_secret=_JWT_SECRET,
        max_bundle_bytes=_MAX_BUNDLE_BYTES,
        default=DeploymentDefault(),
        tenant_id=tenant_id,
        account_id=account_id,
        slug="q3",
        title="t",
        recipients=[],
        cap_usd=Decimal("2.00"),
        agent="alpha",
        now=NOW,
    )

    reader_variant = _reader_agent_dict(
        id_="ag_reader", name="alpha-reader", tenant_id=tenant_id, metadata=created["metadata"]
    )
    # No POST /v1/agents route registered on this second router — a stray
    # second agents.create call raises AssertionError from MARouter.
    router2 = _ma_router([source, reader_variant], tenant_id=tenant_id)
    anthropic2 = build_fake_anthropic(router2.dispatch)

    await publish_report(
        anthropic=anthropic2,
        session_factory=db_session_factory,
        http_client=_host_client(host_handler),
        report_host_settings=_settings(),
        jwt_secret=_JWT_SECRET,
        max_bundle_bytes=_MAX_BUNDLE_BYTES,
        default=DeploymentDefault(),
        tenant_id=tenant_id,
        account_id=account_id,
        slug="q3",
        title="t",
        recipients=[],
        cap_usd=Decimal("2.00"),
        agent="alpha",
        now=NOW,
    )

    async with db_session_factory() as session:
        rows = await list_live_tokens_by_label(session, tenant_id=tenant_id, label="report:q3")
    assert len(rows) == 2, "re-publish mints a second live token; the first is left live"


# ---------------------------------------------------------------------------
# delete_report
# ---------------------------------------------------------------------------


async def test_delete_report_revokes_two_live_tokens_and_calls_host_delete_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        row1 = await make_mcp_token(
            session, tenant=tenant, account=account, label="report:q3", jti=uuid.uuid4()
        )
        row2 = await make_mcp_token(
            session, tenant=tenant, account=account, label="report:q3", jti=uuid.uuid4()
        )
        await session.commit()

    calls: list[httpx.Request] = []

    def host_handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"deleted": True})

    result = await delete_report(
        session_factory=db_session_factory,
        http_client=_host_client(host_handler),
        report_host_settings=_settings(),
        tenant_id=tenant.id,
        slug="q3",
        now=NOW,
    )

    assert result.tokens_revoked == 2
    assert result.host_removed is True
    assert len(calls) == 1
    async with db_session_factory() as session:
        r1 = await get_mcp_token(session, jti=row1.jti)
        r2 = await get_mcp_token(session, jti=row2.jti)
    assert r1 is not None and r1.revoked_at is not None
    assert r2 is not None and r2.revoked_at is not None


async def test_delete_report_revoke_happens_before_the_host_call(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed host call must still leave the token revoked — proves revoke
    runs first (not merely that both eventually happen)."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        row = await make_mcp_token(
            session, tenant=tenant, account=account, label="report:q3", jti=uuid.uuid4()
        )
        await session.commit()

    def failing_host_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(ReportHostError):
        await delete_report(
            session_factory=db_session_factory,
            http_client=_host_client(failing_host_handler),
            report_host_settings=_settings(),
            tenant_id=tenant.id,
            slug="q3",
            now=NOW,
        )

    async with db_session_factory() as session:
        refreshed = await get_mcp_token(session, jti=row.jti)
    assert refreshed is not None and refreshed.revoked_at is not None, (
        "revoke must happen before the host call, not after — a failed host call "
        "must still leave the token revoked"
    )


async def test_delete_report_does_not_touch_a_same_label_token_in_another_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        tenant_a = await make_tenant(session, workspace_id="guild-a")
        tenant_b = await make_tenant(session, workspace_id="guild-b")
        account_a = await make_account(session, tenant=tenant_a)
        account_b = await make_account(session, tenant=tenant_b)
        row_a = await make_mcp_token(
            session, tenant=tenant_a, account=account_a, label="report:q3", jti=uuid.uuid4()
        )
        row_b = await make_mcp_token(
            session, tenant=tenant_b, account=account_b, label="report:q3", jti=uuid.uuid4()
        )
        await session.commit()

    result = await delete_report(
        session_factory=db_session_factory,
        http_client=_host_client(lambda _req: httpx.Response(200, json={"deleted": True})),
        report_host_settings=_settings(),
        tenant_id=tenant_a.id,
        slug="q3",
        now=NOW,
    )

    assert result.tokens_revoked == 1
    async with db_session_factory() as session:
        ra = await get_mcp_token(session, jti=row_a.jti)
        rb = await get_mcp_token(session, jti=row_b.jti)
    assert ra is not None and ra.revoked_at is not None
    assert rb is not None and rb.revoked_at is None, (
        "a token sharing this label in another tenant must be untouched"
    )


async def test_delete_report_with_no_tokens_and_no_host_row_reports_nothing_happened(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id = await _seed_tenant(db_session_factory)

    result = await delete_report(
        session_factory=db_session_factory,
        http_client=_host_client(lambda _req: httpx.Response(200, json={"deleted": False})),
        report_host_settings=_settings(),
        tenant_id=tenant_id,
        slug="ghost",
        now=NOW,
    )

    assert result == DeleteResult(tokens_revoked=0, host_removed=False)


async def test_delete_report_twice_is_idempotent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_mcp_token(
            session, tenant=tenant, account=account, label="report:q3", jti=uuid.uuid4()
        )
        await session.commit()

    calls: list[httpx.Request] = []

    def host_handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(200, json={"deleted": False})

    first = await delete_report(
        session_factory=db_session_factory,
        http_client=_host_client(host_handler),
        report_host_settings=_settings(),
        tenant_id=tenant.id,
        slug="q3",
        now=NOW,
    )
    second = await delete_report(
        session_factory=db_session_factory,
        http_client=_host_client(host_handler),
        report_host_settings=_settings(),
        tenant_id=tenant.id,
        slug="q3",
        now=NOW,
    )

    assert first.tokens_revoked == 1
    assert first.host_removed is True
    assert second.tokens_revoked == 0
    assert second.host_removed is False, (
        "deleting a slug already deleted must not raise or over-report"
    )

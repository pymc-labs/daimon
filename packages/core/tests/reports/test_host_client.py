"""Tests for daimon.core.reports.host_client.

Transport-level mocking via httpx.MockTransport — no AsyncMock on httpx methods.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest
from daimon.core.config import ReportHostSettings
from daimon.core.reports.host_client import (
    Recipient,
    ReportHostError,
    ReportRegistered,
    delete_admin_report,
    put_admin_report,
    revoke_admin_recipient,
)
from pydantic import HttpUrl, SecretStr

pytestmark = pytest.mark.asyncio

_HOST_URL = HttpUrl("http://report-host:8002")
_ADMIN_SECRET = SecretStr("supersecret")
_SLUG = "q3-financials"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _settings(
    *, host_url: HttpUrl | None = _HOST_URL, admin_secret: SecretStr | None = _ADMIN_SECRET
) -> ReportHostSettings:
    return ReportHostSettings(host_url=host_url, admin_secret=admin_secret)


async def test_put_admin_report_sends_correct_path_method_and_bearer() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["auth"] = req.headers.get("Authorization", "")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"slug": _SLUG, "links": []})

    async with _make_client(handler) as client:
        await put_admin_report(
            client=client,
            settings=_settings(),
            slug=_SLUG,
            title="Q3 financials",
            tenant_id=_TENANT_ID,
            agent_name="analyst",
            cap_usd=Decimal("2.00"),
            seam_token="seam-tok",
            recipients=[],
        )

    assert captured["url"] == f"http://report-host:8002/admin/reports/{_SLUG}"
    assert captured["method"] == "PUT"
    assert captured["auth"] == "Bearer supersecret"


async def test_put_admin_report_body_carries_expected_fields_with_string_cap() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"slug": _SLUG, "links": []})

    async with _make_client(handler) as client:
        await put_admin_report(
            client=client,
            settings=_settings(),
            slug=_SLUG,
            title="Q3 financials",
            tenant_id=_TENANT_ID,
            agent_name="analyst",
            cap_usd=Decimal("2.00"),
            seam_token="seam-tok",
            recipients=[
                Recipient(name="Ada", label="ada-link"),
                Recipient(name="Ben", label="ben-link"),
            ],
        )

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["title"] == "Q3 financials"
    assert body["tenant_id"] == str(_TENANT_ID)
    assert body["agent_name"] == "analyst"
    assert body["seam_token"] == "seam-tok"
    assert body["cap_usd"] == "2.00", "cap_usd must be serialised as a string, never a float"
    assert isinstance(body["cap_usd"], str)
    assert body["recipients"] == [
        {"name": "Ada", "label": "ada-link"},
        {"name": "Ben", "label": "ben-link"},
    ]


async def test_put_admin_report_parses_2xx_into_report_registered() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "slug": _SLUG,
                "links": [
                    {"name": "Ada", "link": "https://r.example/ada"},
                    {"name": "Ben", "link": "https://r.example/ben"},
                ],
            },
        )

    async with _make_client(handler) as client:
        result = await put_admin_report(
            client=client,
            settings=_settings(),
            slug=_SLUG,
            title="Q3 financials",
            tenant_id=_TENANT_ID,
            agent_name="analyst",
            cap_usd=Decimal("2.00"),
            seam_token="seam-tok",
            recipients=[Recipient(name="Ada", label="a"), Recipient(name="Ben", label="b")],
        )

    assert result == ReportRegistered(
        slug=_SLUG,
        links={"Ada": "https://r.example/ada", "Ben": "https://r.example/ben"},
    )


async def test_put_admin_report_raises_on_non_2xx_with_status_in_message() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="this slug belongs to a different tenant")

    async with _make_client(handler) as client:
        with pytest.raises(ReportHostError, match="403"):
            await put_admin_report(
                client=client,
                settings=_settings(),
                slug=_SLUG,
                title="t",
                tenant_id=_TENANT_ID,
                agent_name="analyst",
                cap_usd=Decimal("2.00"),
                seam_token="seam-tok",
                recipients=[],
            )


async def test_put_admin_report_transport_error_raises_with_cause_preserved() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _make_client(handler) as client:
        with pytest.raises(ReportHostError) as excinfo:
            await put_admin_report(
                client=client,
                settings=_settings(),
                slug=_SLUG,
                title="t",
                tenant_id=_TENANT_ID,
                agent_name="analyst",
                cap_usd=Decimal("2.00"),
                seam_token="seam-tok",
                recipients=[],
            )
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError), (
        "the transport failure must be preserved as the exception cause"
    )


async def test_put_admin_report_unset_host_url_raises_before_any_request() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"slug": _SLUG, "links": []})

    async with _make_client(handler) as client:
        with pytest.raises(ReportHostError, match="host_url"):
            await put_admin_report(
                client=client,
                settings=_settings(host_url=None),
                slug=_SLUG,
                title="t",
                tenant_id=_TENANT_ID,
                agent_name="analyst",
                cap_usd=Decimal("2.00"),
                seam_token="seam-tok",
                recipients=[],
            )
    assert seen == [], "an unconfigured host_url must never issue a request"


async def test_put_admin_report_unset_admin_secret_raises_before_any_request() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"slug": _SLUG, "links": []})

    async with _make_client(handler) as client:
        with pytest.raises(ReportHostError, match="admin_secret"):
            await put_admin_report(
                client=client,
                settings=_settings(admin_secret=None),
                slug=_SLUG,
                title="t",
                tenant_id=_TENANT_ID,
                agent_name="analyst",
                cap_usd=Decimal("2.00"),
                seam_token="seam-tok",
                recipients=[],
            )
    assert seen == [], "an unconfigured admin_secret must never issue a request"


async def test_put_admin_report_response_missing_field_raises() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        # Missing "links" entirely — a shape mismatch, not a partial result.
        return httpx.Response(200, json={"slug": _SLUG})

    async with _make_client(handler) as client:
        with pytest.raises(ReportHostError):
            await put_admin_report(
                client=client,
                settings=_settings(),
                slug=_SLUG,
                title="t",
                tenant_id=_TENANT_ID,
                agent_name="analyst",
                cap_usd=Decimal("2.00"),
                seam_token="seam-tok",
                recipients=[],
            )


async def test_delete_admin_report_sends_tenant_id_query_param_and_bearer() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["auth"] = req.headers.get("Authorization", "")
        return httpx.Response(200, json={"deleted": True})

    async with _make_client(handler) as client:
        deleted = await delete_admin_report(
            client=client, settings=_settings(), slug=_SLUG, tenant_id=_TENANT_ID
        )

    assert captured["method"] == "DELETE"
    assert (
        captured["url"] == f"http://report-host:8002/admin/reports/{_SLUG}?tenant_id={_TENANT_ID}"
    )
    assert captured["auth"] == "Bearer supersecret"
    assert deleted is True


async def test_delete_admin_report_returns_false_when_host_removed_nothing() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"deleted": False})

    async with _make_client(handler) as client:
        deleted = await delete_admin_report(
            client=client, settings=_settings(), slug=_SLUG, tenant_id=_TENANT_ID
        )
    assert deleted is False


async def test_delete_admin_report_raises_on_non_2xx() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _make_client(handler) as client:
        with pytest.raises(ReportHostError, match="500"):
            await delete_admin_report(
                client=client, settings=_settings(), slug=_SLUG, tenant_id=_TENANT_ID
            )


async def test_delete_admin_report_unset_settings_raises_before_any_request() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"deleted": True})

    async with _make_client(handler) as client:
        with pytest.raises(ReportHostError):
            await delete_admin_report(
                client=client,
                settings=_settings(host_url=None, admin_secret=None),
                slug=_SLUG,
                tenant_id=_TENANT_ID,
            )
    assert seen == []


async def test_revoke_admin_recipient_deletes_by_token() -> None:
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        return httpx.Response(200, json={"deleted": True})

    async with _make_client(handler) as client:
        revoked = await revoke_admin_recipient(
            client=client, settings=_settings(), slug=_SLUG, token="tok-1"
        )

    assert captured["method"] == "DELETE"
    assert captured["url"] == f"http://report-host:8002/admin/reports/{_SLUG}/recipients/tok-1"
    assert revoked is True

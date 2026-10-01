"""Tests for the admin router: publish (create-or-update), delete, revoke.

Drives `build_admin_router` under `httpx.ASGITransport` with a real SQLite
file in `tmp_path` and two configured admin secrets (proving the CSV
rotation property). Never imports daimon.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from report_host import reports_store
from report_host.admin import (
    _validate_slug,  # pyright: ignore[reportPrivateUsage]
    build_admin_router,
)
from report_host.config import Settings, load_settings

AUTH = "Bearer admin-secret"
BACKUP_AUTH = "Bearer backup-secret"
WRONG_AUTH = "Bearer wrong-secret"


def _settings(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "admin-secret,backup-secret")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://testserver/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "http://reports.example.com")
    settings = load_settings(_env_file=None)
    fields: dict[str, object] = {"data_dir": tmp_path / "data"}
    fields.update(overrides)
    return settings.model_copy(update=fields)


def _make_app(*, settings: Settings, conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI()
    app.include_router(build_admin_router(settings=settings, conn_factory=lambda: conn))
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _publish_body(
    *,
    title: str = "Acme Q3",
    tenant_id: str = "tenant-1",
    agent_name: str = "analyst",
    cap_usd: str = "10",
    seam_token: str = "seam-token-secret-1",
    recipients: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "title": title,
        "tenant_id": tenant_id,
        "agent_name": agent_name,
        "cap_usd": cap_usd,
        "seam_token": seam_token,
        "recipients": (
            recipients if recipients is not None else [{"name": "Jane", "label": "reader"}]
        ),
    }


# --------------------------------------------------------------------------
# Slug validation (pure, unit-level — real slashes can't reach the handler
# through single-segment HTTP routing, so this path is asserted directly)
# --------------------------------------------------------------------------


def test_validate_slug_rejects_a_slug_containing_a_path_separator() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _validate_slug("foo/bar")
    assert excinfo.value.status_code in (400, 422)


def test_validate_slug_rejects_dotdot() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _validate_slug("..")
    assert excinfo.value.status_code in (400, 422)


def test_validate_slug_rejects_uppercase() -> None:
    with pytest.raises(HTTPException) as excinfo:
        _validate_slug("Acme")
    assert excinfo.value.status_code in (400, 422)


def test_validate_slug_accepts_a_conservative_slug() -> None:
    assert _validate_slug("acme-q3") == "acme-q3"


# --------------------------------------------------------------------------
# Bearer rejection and rotation
# --------------------------------------------------------------------------


async def test_put_without_authorization_header_returns_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        resp = await client.put("/admin/reports/acme", json=_publish_body())
    assert resp.status_code == 401


async def test_delete_report_without_authorization_header_returns_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        resp = await client.delete("/admin/reports/acme", params={"tenant_id": "tenant-1"})
    assert resp.status_code == 401


async def test_delete_recipient_without_authorization_header_returns_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        resp = await client.delete("/admin/reports/acme/recipients/tok-1")
    assert resp.status_code == 401


async def test_put_with_wrong_bearer_returns_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        resp = await client.put(
            "/admin/reports/acme",
            json=_publish_body(),
            headers={"authorization": WRONG_AUTH},
        )
    assert resp.status_code == 401


async def test_put_with_second_configured_secret_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup secret in the CSV rotation list is honoured, not just the first."""
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        resp = await client.put(
            "/admin/reports/acme",
            json=_publish_body(),
            headers={"authorization": BACKUP_AUTH},
        )
    assert resp.status_code == 200, "the second configured admin secret should be accepted"


# --------------------------------------------------------------------------
# Publish: create, replace recipients, tenant ownership, spend preservation
# --------------------------------------------------------------------------


async def test_put_creates_report_and_returns_one_link_per_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    body = _publish_body(
        recipients=[{"name": "Jane", "label": "reader"}, {"name": "Bob", "label": "reader"}]
    )
    async with await _client(app) as client:
        resp = await client.put("/admin/reports/acme", json=body, headers={"authorization": AUTH})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["slug"] == "acme"
    links = payload["links"]
    assert len(links) == 2
    tokens: set[str] = set()
    for link in links:
        assert link["link"].startswith("http://reports.example.com/r/acme?k=")
        token = link["link"].rsplit("k=", 1)[1]
        tokens.add(token)
    assert len(tokens) == 2, "each recipient must get a distinct token"
    assert "seam_token" not in payload, "the seam token must never be echoed back"

    report = reports_store.load_report(conn, slug="acme")
    assert report is not None
    assert report.title == "Acme Q3"
    assert report.tenant_id == "tenant-1"


async def test_second_put_same_tenant_updates_title_and_replaces_recipients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        first = await client.put(
            "/admin/reports/acme", json=_publish_body(title="Q3"), headers={"authorization": AUTH}
        )
        old_token = first.json()["links"][0]["link"].rsplit("k=", 1)[1]

        second = await client.put(
            "/admin/reports/acme",
            json=_publish_body(title="Q4", recipients=[{"name": "New Reader", "label": "reader"}]),
            headers={"authorization": AUTH},
        )
    assert second.status_code == 200
    report = reports_store.load_report(conn, slug="acme")
    assert report is not None
    assert report.title == "Q4"

    assert (
        reports_store.load_recipient(conn, slug="acme", token=old_token, now=datetime.now(UTC))
        is None
    ), "the previously issued link must stop resolving after a re-publish"
    new_recipients = [
        r for r in reports_store.list_recipients(conn, slug="acme") if r.revoked_at is None
    ]
    assert len(new_recipients) == 1
    assert new_recipients[0].name == "New Reader"


async def test_second_put_same_tenant_preserves_spend_pdf_and_bundle_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-publishing metadata must not refund the cap or blank a served PDF.

    Mutation-checked: making `save_report` reset `spent_usd` on conflict
    (temporarily editing `reports_store.py`'s `ON CONFLICT` clause to add
    `spent_usd = 0` and re-running this test) turns this assertion red;
    reverting turns it green again.
    """
    conn = reports_store.connect(tmp_path / "data")
    reports_store.save_report(
        conn,
        slug="acme",
        title="Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("10"),
        agent_token="seam-token-secret-1",
        now=datetime.now(UTC),
    )
    reports_store.reserve_budget(conn, slug="acme", amount=Decimal("2"))
    reports_store.set_current_pdf(conn, slug="acme", name="report.pdf")
    reports_store.save_bundle_reference(
        conn,
        slug="acme",
        handle="bundle-handle-1",
        sha256="a" * 64,
        expires_at=datetime.now(UTC),
        archive_path="acme/archive.tar.gz",
    )

    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        resp = await client.put(
            "/admin/reports/acme", json=_publish_body(title="Q4"), headers={"authorization": AUTH}
        )
    assert resp.status_code == 200

    report = reports_store.load_report(conn, slug="acme")
    assert report is not None
    assert report.spent_usd == Decimal("2"), "a metadata re-publish must not reset spend"
    assert report.current_pdf == "report.pdf", "a metadata re-publish must not blank the served PDF"
    assert report.bundle_handle == "bundle-handle-1", (
        "a metadata re-publish must not orphan the bundle reference"
    )


async def test_put_from_different_tenant_returns_403_and_leaves_existing_row_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slug squatting: a different tenant's PUT must not take over the slug.

    Mutation-checked: commenting out the tenant-mismatch check in
    `admin.py`'s `put_report` turns this assertion red (the second PUT would
    succeed and silently rewrite `tenant_id`/`title`); restoring it turns it
    green again.
    """
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        await client.put(
            "/admin/reports/acme",
            json=_publish_body(title="Q3", tenant_id="tenant-1"),
            headers={"authorization": AUTH},
        )
        resp = await client.put(
            "/admin/reports/acme",
            json=_publish_body(title="Hijacked", tenant_id="tenant-2"),
            headers={"authorization": AUTH},
        )
    assert resp.status_code == 403
    assert "tenant-1" not in resp.text and "tenant-2" not in resp.text, (
        "the refusal must not reveal either tenant"
    )

    report = reports_store.load_report(conn, slug="acme")
    assert report is not None
    assert report.title == "Q3", "the existing row must be unchanged, not merely refused"
    assert report.tenant_id == "tenant-1"


async def test_put_rejects_slug_with_uppercase_and_creates_no_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    app = _make_app(settings=settings, conn=conn)
    async with await _client(app) as client:
        resp = await client.put(
            "/admin/reports/Acme", json=_publish_body(), headers={"authorization": AUTH}
        )
    assert resp.status_code in (400, 422)
    assert not (settings.data_dir / "Acme").exists()
    assert reports_store.load_report(conn, slug="Acme") is None


async def test_put_rejects_a_percent_encoded_dotdot_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    app = _make_app(settings=settings, conn=conn)
    async with await _client(app) as client:
        resp = await client.put(
            "/admin/reports/%2e%2e", json=_publish_body(), headers={"authorization": AUTH}
        )
    assert resp.status_code in (400, 422)
    assert reports_store.load_report(conn, slug="..") is None


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


async def test_delete_with_matching_tenant_removes_report_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    app = _make_app(settings=settings, conn=conn)
    report_dir = settings.data_dir / "acme"
    report_dir.mkdir(parents=True)
    (report_dir / "report.pdf").write_bytes(b"%PDF-1.4")

    async with await _client(app) as client:
        await client.put(
            "/admin/reports/acme", json=_publish_body(), headers={"authorization": AUTH}
        )
        resp = await client.delete(
            "/admin/reports/acme",
            params={"tenant_id": "tenant-1"},
            headers={"authorization": AUTH},
        )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert reports_store.load_report(conn, slug="acme") is None
    assert not report_dir.exists()


async def test_delete_with_mismatched_tenant_returns_403_and_report_still_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        await client.put(
            "/admin/reports/acme",
            json=_publish_body(tenant_id="tenant-1"),
            headers={"authorization": AUTH},
        )
        resp = await client.delete(
            "/admin/reports/acme",
            params={"tenant_id": "tenant-2"},
            headers={"authorization": AUTH},
        )
    assert resp.status_code == 403
    assert reports_store.load_report(conn, slug="acme") is not None


async def test_delete_of_unknown_slug_reports_nothing_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        resp = await client.delete(
            "/admin/reports/ghost",
            params={"tenant_id": "tenant-1"},
            headers={"authorization": AUTH},
        )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is False


async def test_successful_and_unknown_slug_deletes_report_different_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        await client.put(
            "/admin/reports/acme",
            json=_publish_body(tenant_id="tenant-1"),
            headers={"authorization": AUTH},
        )
        success = await client.delete(
            "/admin/reports/acme",
            params={"tenant_id": "tenant-1"},
            headers={"authorization": AUTH},
        )
        unknown = await client.delete(
            "/admin/reports/acme",
            params={"tenant_id": "tenant-1"},
            headers={"authorization": AUTH},
        )
    assert success.json()["deleted"] != unknown.json()["deleted"], (
        "a real deletion must be distinguishable from a no-op delete of the same slug"
    )


# --------------------------------------------------------------------------
# Recipient revocation
# --------------------------------------------------------------------------


async def test_recipient_revoke_stops_that_link_while_siblings_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        published = await client.put(
            "/admin/reports/acme",
            json=_publish_body(
                recipients=[{"name": "Jane", "label": "reader"}, {"name": "Bob", "label": "reader"}]
            ),
            headers={"authorization": AUTH},
        )
        links = published.json()["links"]
        jane_token = next(link for link in links if link["name"] == "Jane")["link"].rsplit("k=", 1)[
            1
        ]
        bob_token = next(link for link in links if link["name"] == "Bob")["link"].rsplit("k=", 1)[1]

        resp = await client.delete(
            f"/admin/reports/acme/recipients/{jane_token}", headers={"authorization": AUTH}
        )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    now = datetime.now(UTC)
    assert reports_store.load_recipient(conn, slug="acme", token=jane_token, now=now) is None
    assert reports_store.load_recipient(conn, slug="acme", token=bob_token, now=now) is not None


async def test_revoking_the_same_recipient_twice_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    app = _make_app(settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch), conn=conn)
    async with await _client(app) as client:
        published = await client.put(
            "/admin/reports/acme", json=_publish_body(), headers={"authorization": AUTH}
        )
        token = published.json()["links"][0]["link"].rsplit("k=", 1)[1]

        first = await client.delete(
            f"/admin/reports/acme/recipients/{token}", headers={"authorization": AUTH}
        )
        second = await client.delete(
            f"/admin/reports/acme/recipients/{token}", headers={"authorization": AUTH}
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["deleted"] is True
    assert second.json()["deleted"] is False

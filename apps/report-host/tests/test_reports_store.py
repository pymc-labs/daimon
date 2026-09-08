"""Tests for report_host.reports_store — reports, recipients and revisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from report_host import reports_store

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _connect(tmp_path: Path):
    return reports_store.connect(tmp_path / "data")


def test_save_report_then_load_round_trips(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    saved = reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    assert saved.slug == "acme-q3"
    assert saved.spent_usd == Decimal("0")
    assert saved.seam_status == "ok"

    loaded = reports_store.load_report(conn, slug="acme-q3")
    assert loaded is not None, "report should be durable across a fresh load"
    assert loaded == saved


def test_load_report_returns_none_for_unknown_slug(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    assert reports_store.load_report(conn, slug="nope") is None


def test_resave_report_preserves_spend_current_pdf_and_bundle_fields(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.reserve_budget(conn, slug="acme-q3", amount=Decimal("1.50"))
    reports_store.set_current_pdf(conn, slug="acme-q3", name="v1.pdf")
    reports_store.save_bundle_reference(
        conn,
        slug="acme-q3",
        handle="bundle-handle-1",
        sha256="a" * 64,
        expires_at=NOW + timedelta(days=90),
        archive_path="/data/reports/acme-q3/archive.tar.gz",
    )

    resaved = reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3 (revised title)",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-2",
        now=NOW + timedelta(days=1),
    )

    assert resaved.title == "Acme Q3 (revised title)"
    assert resaved.seam_token == "seam-token-2"
    assert resaved.spent_usd == Decimal("1.50"), "re-save must not zero an existing spend"
    assert resaved.current_pdf == "v1.pdf", "re-save must not orphan the published PDF"
    assert resaved.bundle_handle == "bundle-handle-1"
    assert resaved.bundle_sha256 == "a" * 64
    assert resaved.archive_path == "/data/reports/acme-q3/archive.tar.gz", (
        "a subsequent save_report must leave archive_path intact"
    )


def test_save_bundle_reference_round_trips_archive_path(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.save_bundle_reference(
        conn,
        slug="acme-q3",
        handle="bundle-handle-1",
        sha256="b" * 64,
        expires_at=NOW + timedelta(days=90),
        archive_path="/data/reports/acme-q3/archive.tar.gz",
    )
    loaded = reports_store.load_report(conn, slug="acme-q3")
    assert loaded is not None
    assert loaded.archive_path == "/data/reports/acme-q3/archive.tar.gz"
    assert loaded.bundle_handle == "bundle-handle-1"
    assert loaded.bundle_sha256 == "b" * 64
    assert loaded.bundle_expires_at == NOW + timedelta(days=90)


def test_delete_report_returns_true_once_then_false(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    assert reports_store.delete_report(conn, slug="acme-q3") is True
    assert reports_store.delete_report(conn, slug="acme-q3") is False
    assert reports_store.load_report(conn, slug="acme-q3") is None


def test_delete_report_returns_false_for_unknown_slug(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    assert reports_store.delete_report(conn, slug="never-existed") is False


def test_reserve_budget_succeeds_under_cap(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    new_spend = reports_store.reserve_budget(conn, slug="acme-q3", amount=Decimal("2.00"))
    assert new_spend == Decimal("2.00")


def test_reserve_budget_returns_none_exactly_at_cap_boundary(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("2.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    # Exactly at the cap succeeds (<=); one cent over fails.
    assert reports_store.reserve_budget(conn, slug="acme-q3", amount=Decimal("2.00")) == Decimal(
        "2.00"
    )
    assert reports_store.reserve_budget(conn, slug="acme-q3", amount=Decimal("0.01")) is None


def test_two_sequential_reserves_exceeding_cap_leave_second_refused_and_spend_unchanged(
    tmp_path: Path,
) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("3.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    first = reports_store.reserve_budget(conn, slug="acme-q3", amount=Decimal("2.00"))
    assert first == Decimal("2.00")
    second = reports_store.reserve_budget(conn, slug="acme-q3", amount=Decimal("2.00"))
    assert second is None, "a reserve that would cross the cap must be refused"
    loaded = reports_store.load_report(conn, slug="acme-q3")
    assert loaded is not None
    assert loaded.spent_usd == Decimal("2.00"), "a refused reserve must not change the spend"


def test_settle_budget_replaces_reservation_with_smaller_actual(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.reserve_budget(conn, slug="acme-q3", amount=Decimal("0.60"))
    reports_store.settle_budget(
        conn, slug="acme-q3", reserved=Decimal("0.60"), actual=Decimal("0.23")
    )
    loaded = reports_store.load_report(conn, slug="acme-q3")
    assert loaded is not None
    assert loaded.spent_usd == Decimal("0.23")


def test_settle_budget_with_actual_none_leaves_reservation_in_place(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.reserve_budget(conn, slug="acme-q3", amount=Decimal("0.60"))
    reports_store.settle_budget(conn, slug="acme-q3", reserved=Decimal("0.60"), actual=None)
    loaded = reports_store.load_report(conn, slug="acme-q3")
    assert loaded is not None
    assert loaded.spent_usd == Decimal("0.60"), (
        "an unpriced turn is not a zero-cost turn; the reservation must stand"
    )


def test_recipient_past_expiry_loads_as_none(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.add_recipient(
        conn, slug="acme-q3", name="Jane", label="CFO", token="tok-1", now=NOW, ttl_days=1
    )
    still_valid = reports_store.load_recipient(
        conn, slug="acme-q3", token="tok-1", now=NOW + timedelta(hours=12)
    )
    assert still_valid is not None
    expired = reports_store.load_recipient(
        conn, slug="acme-q3", token="tok-1", now=NOW + timedelta(days=2)
    )
    assert expired is None


def test_revoked_recipient_loads_as_none(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.add_recipient(
        conn, slug="acme-q3", name="Jane", label="CFO", token="tok-1", now=NOW, ttl_days=90
    )
    assert reports_store.revoke_recipient(conn, slug="acme-q3", token="tok-1", now=NOW) is True
    assert reports_store.revoke_recipient(conn, slug="acme-q3", token="tok-1", now=NOW) is False, (
        "revoking an already-revoked token is a no-op, not a second success"
    )
    assert reports_store.load_recipient(conn, slug="acme-q3", token="tok-1", now=NOW) is None


def test_list_recipients_lists_all_for_slug(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.add_recipient(
        conn, slug="acme-q3", name="Jane", label="CFO", token="tok-1", now=NOW, ttl_days=90
    )
    reports_store.add_recipient(
        conn,
        slug="acme-q3",
        name="Bob",
        label="CEO",
        token="tok-2",
        now=NOW + timedelta(seconds=1),
        ttl_days=90,
    )
    recipients = reports_store.list_recipients(conn, slug="acme-q3")
    assert [r.token for r in recipients] == ["tok-1", "tok-2"]


def test_revisions_list_in_creation_order(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.add_revision(
        conn, slug="acme-q3", name="v1.pdf", by_thread=None, note="published", now=NOW
    )
    reports_store.add_revision(
        conn,
        slug="acme-q3",
        name="v2.pdf",
        by_thread="thread-1",
        note="revised",
        now=NOW + timedelta(minutes=5),
    )
    revisions = reports_store.list_revisions(conn, slug="acme-q3")
    assert [r.name for r in revisions] == ["v1.pdf", "v2.pdf"]


def test_set_seam_status_persists(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    reports_store.save_report(
        conn,
        slug="acme-q3",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    reports_store.set_seam_status(conn, slug="acme-q3", status="unauthorized")
    loaded = reports_store.load_report(conn, slug="acme-q3")
    assert loaded is not None
    assert loaded.seam_status == "unauthorized"

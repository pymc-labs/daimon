"""Tests for report_host.threads_store — threads, turn boundaries, messages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from report_host import reports_store, threads_store

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _connect_with_report(tmp_path: Path, slug: str = "acme-q3"):
    conn = reports_store.connect(tmp_path / "data")
    reports_store.save_report(
        conn,
        slug=slug,
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-1",
        now=NOW,
    )
    return conn


def test_create_thread_then_load_round_trips(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    created = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Why did revenue drop?", now=NOW
    )
    assert created.status == "idle"
    loaded = threads_store.load_thread(
        conn, thread_id=created.id, slug="acme-q3", recipient_token="tok-1"
    )
    assert loaded == created


def test_load_thread_returns_none_for_wrong_recipient_token(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    created = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q", now=NOW
    )
    assert (
        threads_store.load_thread(
            conn, thread_id=created.id, slug="acme-q3", recipient_token="someone-elses-token"
        )
        is None
    )


def test_load_thread_returns_none_for_wrong_slug(tmp_path: Path) -> None:
    conn = reports_store.connect(tmp_path / "data")
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
    reports_store.save_report(
        conn,
        slug="other-report",
        title="Other",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=Decimal("5.00"),
        agent_token="seam-token-2",
        now=NOW,
    )
    created = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q", now=NOW
    )
    assert (
        threads_store.load_thread(
            conn, thread_id=created.id, slug="other-report", recipient_token="tok-1"
        )
        is None
    )


def test_begin_turn_returns_true_once_then_false_while_running(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    created = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q", now=NOW
    )
    started = threads_store.begin_turn(
        conn,
        thread_id=created.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=1200),
        now=NOW,
    )
    assert started is True
    still_running = threads_store.begin_turn(
        conn,
        thread_id=created.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=1200),
        now=NOW,
    )
    assert still_running is False, "a second question must be refused while one is running"


def test_end_turn_clears_boundary_reservation_and_deadline(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    created = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q", now=NOW
    )
    threads_store.begin_turn(
        conn,
        thread_id=created.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=1200),
        now=NOW,
    )
    threads_store.bind_turn_boundary(
        conn,
        thread_id=created.id,
        handle="session-abc",
        turn_event_id="evt-1",
        turn_started_at=NOW,
    )
    threads_store.end_turn(conn, thread_id=created.id, now=NOW + timedelta(minutes=1))

    loaded = threads_store.load_thread(
        conn, thread_id=created.id, slug="acme-q3", recipient_token="tok-1"
    )
    assert loaded is not None
    assert loaded.status == "idle"
    assert loaded.turn_started_at is None
    assert loaded.turn_event_id is None
    assert loaded.turn_deadline_at is None
    assert loaded.reserved_usd is None
    assert loaded.handle == "session-abc", "the seam session handle survives past the turn"


def test_bind_turn_boundary_records_handle_and_event(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    created = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q", now=NOW
    )
    threads_store.bind_turn_boundary(
        conn,
        thread_id=created.id,
        handle="session-abc",
        turn_event_id="evt-1",
        turn_started_at=NOW,
    )
    loaded = threads_store.load_thread(
        conn, thread_id=created.id, slug="acme-q3", recipient_token="tok-1"
    )
    assert loaded is not None
    assert loaded.handle == "session-abc"
    assert loaded.turn_event_id == "evt-1"
    assert loaded.turn_started_at == NOW


def test_count_open_threads_and_count_running_turns_respect_scope(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    t1 = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q1", now=NOW
    )
    threads_store.create_thread(conn, slug="acme-q3", recipient_token="tok-1", title="Q2", now=NOW)
    threads_store.create_thread(conn, slug="acme-q3", recipient_token="tok-2", title="Q3", now=NOW)

    assert threads_store.count_open_threads(conn, slug="acme-q3", recipient_token="tok-1") == 2
    assert threads_store.count_open_threads(conn, slug="acme-q3", recipient_token="tok-2") == 1

    threads_store.begin_turn(
        conn,
        thread_id=t1.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=1200),
        now=NOW,
    )
    assert threads_store.count_running_turns(conn, slug="acme-q3") == 1


def test_list_running_threads_returns_exactly_the_running_ones(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    t1 = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q1", now=NOW
    )
    threads_store.create_thread(conn, slug="acme-q3", recipient_token="tok-1", title="Q2", now=NOW)
    threads_store.begin_turn(
        conn,
        thread_id=t1.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=1200),
        now=NOW,
    )
    running = threads_store.list_running_threads(conn)
    assert [t.id for t in running] == [t1.id]


def test_add_message_records_bundle_digest_and_revision(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    created = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q", now=NOW
    )
    message = threads_store.add_message(
        conn,
        thread_id=created.id,
        role="assistant",
        text="Revenue dropped because of X.",
        now=NOW,
        bundle_sha256="c" * 64,
        pdf_revision="v2.pdf",
    )
    assert message.bundle_sha256 == "c" * 64
    assert message.pdf_revision == "v2.pdf"


def test_list_messages_after_id_returns_only_newer_rows(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    created = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Q", now=NOW
    )
    m1 = threads_store.add_message(
        conn,
        thread_id=created.id,
        role="user",
        text="Why?",
        now=NOW,
        bundle_sha256=None,
        pdf_revision=None,
    )
    m2 = threads_store.add_message(
        conn,
        thread_id=created.id,
        role="assistant",
        text="Because X.",
        now=NOW + timedelta(seconds=1),
        bundle_sha256="d" * 64,
        pdf_revision="v1.pdf",
    )
    all_messages = threads_store.list_messages(conn, thread_id=created.id)
    assert [m.id for m in all_messages] == [m1.id, m2.id]

    only_new = threads_store.list_messages(conn, thread_id=created.id, after_id=m1.id)
    assert [m.id for m in only_new] == [m2.id]


def test_list_threads_idle_since_excludes_already_archived_threads(tmp_path: Path) -> None:
    conn = _connect_with_report(tmp_path)
    old_idle = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Old idle", now=NOW
    )
    archived = threads_store.create_thread(
        conn, slug="acme-q3", recipient_token="tok-1", title="Archived", now=NOW
    )
    threads_store.archive_thread(conn, thread_id=archived.id, now=NOW + timedelta(hours=1))

    cutoff = NOW + timedelta(hours=24)
    idle = threads_store.list_threads_idle_since(conn, cutoff=cutoff)
    assert [t.id for t in idle] == [old_idle.id]

"""Tests for report_host.sweeps: resume, deadline cancel, idle archive, prune.

A real SQLite file in `tmp_path`, a fake seam (the shared `build_fake_seam` /
`fake_seam_lifespan` fixtures from `conftest.py`) for the two sweeps that
call the seam, and a fake `run_turn` recorder for the resume sweep — resume
is asserted to schedule the right calls, never to drive a real turn end to
end.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from report_host import reports_store, sweeps, threads_store
from report_host.config import Settings, load_settings
from report_host.mcp_client import SeamClient
from starlette.types import Receive, Scope, Send

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

# Local aliases mirroring conftest's fixture types (see test_mcp_client.py for
# why these can't just be imported: `--import-mode=importlib` gives every
# test module its own namespace with no shared sys.path entry).
FakeASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
FakeSeamBuilder = Callable[..., FakeASGIApp]
FakeSeamLifespan = Callable[[FakeASGIApp], AbstractAsyncContextManager[None]]


class _RunTurnRecorder:
    """A fake `run_turn` collaborator: records its call, never drives a turn."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class _RaisingSchedule:
    """A `schedule` collaborator whose first call raises, to prove one bad
    row never aborts the rest of the resume pass."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, coro: Awaitable[None]) -> None:
        self.calls += 1
        coro.close()  # type: ignore[attr-defined]  # never actually scheduled
        if self.calls == 1:
            raise RuntimeError("scheduling blew up")


def _settings(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "admin-secret")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://testserver/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "http://reports.example.com")
    settings = load_settings(_env_file=None)
    fields: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "thread_idle_archive_hours": 24,
    }
    fields.update(overrides)
    return settings.model_copy(update=fields)


def _seed_report(
    conn: sqlite3.Connection, *, slug: str = "acme", cap_usd: Decimal = Decimal("100")
) -> reports_store.ReportRow:
    reports_store.save_report(
        conn,
        slug=slug,
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=cap_usd,
        agent_token="seam-token-1",
        now=NOW,
    )
    report = reports_store.load_report(conn, slug=slug)
    assert report is not None
    return report


def _seed_running_thread(
    conn: sqlite3.Connection,
    *,
    slug: str = "acme",
    with_handle: bool,
    deadline_at: datetime,
    reserved_usd: Decimal = Decimal("0.60"),
) -> threads_store.ThreadRow:
    thread = threads_store.create_thread(
        conn, slug=slug, recipient_token="rcpt-1", title="Q1", now=NOW
    )
    ok = threads_store.begin_turn(
        conn, thread_id=thread.id, reserved_usd=reserved_usd, deadline_at=deadline_at, now=NOW
    )
    assert ok
    if with_handle:
        threads_store.bind_turn_boundary(
            conn, thread_id=thread.id, handle="ses-1", turn_event_id="evt-0", turn_started_at=NOW
        )
    reloaded = threads_store.load_thread(
        conn, thread_id=thread.id, slug=slug, recipient_token="rcpt-1"
    )
    assert reloaded is not None
    return reloaded


def _poisoned_seam() -> SeamClient:
    """A SeamClient no test in this file's resume tests should ever call."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected seam call: {request.url}")

    transport = httpx.MockTransport(handler)
    return SeamClient(
        mcp_url="http://poisoned/mcp", http_client=httpx.AsyncClient(transport=transport)
    )


def _seam_client(app: FakeASGIApp) -> SeamClient:
    import httpx

    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    return SeamClient(
        mcp_url="http://testserver/mcp",
        http_client=httpx.AsyncClient(transport=transport),
        transport=transport,
    )


# --------------------------------------------------------------------------
# resume_running_threads
# --------------------------------------------------------------------------


async def test_resume_schedules_exactly_one_reattach_per_running_thread_with_a_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    thread = _seed_running_thread(conn, with_handle=True, deadline_at=NOW + timedelta(seconds=1200))
    recorder = _RunTurnRecorder()
    scheduled: list[Awaitable[None]] = []

    resumed = await sweeps.resume_running_threads(
        conn=conn,
        seam=_poisoned_seam(),
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        now=lambda: NOW,
        schedule=scheduled.append,
        run_turn=recorder,
    )
    for coro in scheduled:
        await coro  # run the recorder synchronously; it makes no real seam call

    assert resumed == 1
    assert len(recorder.calls) == 1, "exactly one re-attach must be scheduled"
    assert recorder.calls[0]["message"] is None, "a re-attach never sends a new message"
    scheduled_thread = recorder.calls[0]["thread"]
    assert isinstance(scheduled_thread, threads_store.ThreadRow)
    assert scheduled_thread.id == thread.id


async def test_resume_stores_ask_again_and_does_not_schedule_for_a_handleless_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    report = _seed_report(conn)
    reserved = reports_store.reserve_budget(conn, slug=report.slug, amount=Decimal("0.60"))
    assert reserved is not None
    thread = _seed_running_thread(
        conn, with_handle=False, deadline_at=NOW + timedelta(seconds=1200)
    )
    recorder = _RunTurnRecorder()
    scheduled: list[Awaitable[None]] = []

    resumed = await sweeps.resume_running_threads(
        conn=conn,
        seam=_poisoned_seam(),
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        now=lambda: NOW,
        schedule=scheduled.append,
        run_turn=recorder,
    )

    assert resumed == 0
    assert scheduled == [], "a thread that never reached the seam must never be scheduled"
    assert recorder.calls == []
    messages = threads_store.list_messages(conn, thread_id=thread.id)
    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "ask it again" in messages[0].text
    reloaded = threads_store.load_thread(
        conn, thread_id=thread.id, slug=thread.slug, recipient_token=thread.recipient_token
    )
    assert reloaded is not None
    assert reloaded.status == "idle", "a lost question must not be left running forever"
    settled = reports_store.load_report(conn, slug=report.slug)
    assert settled is not None
    assert settled.spent_usd == Decimal("0"), "the reservation for a lost question is released"


async def test_resume_continues_past_a_thread_whose_scheduling_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _bad_thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="rcpt-bad", title="bad", now=NOW
    )
    ok = threads_store.begin_turn(
        conn,
        thread_id=_bad_thread.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=1200),
        now=NOW,
    )
    assert ok
    threads_store.bind_turn_boundary(
        conn,
        thread_id=_bad_thread.id,
        handle="ses-bad",
        turn_event_id="evt-0",
        turn_started_at=NOW,
    )
    _good_thread = _seed_running_thread(
        conn, with_handle=True, deadline_at=NOW + timedelta(seconds=1200)
    )
    recorder = _RunTurnRecorder()
    schedule = _RaisingSchedule()

    resumed = await sweeps.resume_running_threads(
        conn=conn,
        seam=_poisoned_seam(),
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        now=lambda: NOW,
        schedule=schedule,
        run_turn=recorder,
    )

    assert schedule.calls == 2, "the second thread must still be attempted after the first raises"
    assert resumed == 1, "only the successfully scheduled thread is counted"


# --------------------------------------------------------------------------
# cancel_expired_turns
# --------------------------------------------------------------------------


async def test_cancel_expired_turns_calls_seam_once_per_expired_turn_and_none_inside_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    expired = _seed_running_thread(conn, with_handle=True, deadline_at=NOW - timedelta(seconds=1))
    threads_store.create_thread(conn, slug="acme", recipient_token="rcpt-2", title="Q2", now=NOW)
    within_deadline_thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="rcpt-3", title="Q3", now=NOW
    )
    ok = threads_store.begin_turn(
        conn,
        thread_id=within_deadline_thread.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=1200),
        now=NOW,
    )
    assert ok
    threads_store.bind_turn_boundary(
        conn,
        thread_id=within_deadline_thread.id,
        handle="ses-within",
        turn_event_id="evt-0",
        turn_started_at=NOW,
    )

    cancel_calls: list[dict[str, object]] = []

    def cancel_turn(args: dict[str, object]) -> dict[str, object]:
        cancel_calls.append(args)
        return {"handle": args["handle"], "status": "running"}

    app = build_fake_seam(behaviors={"cancel_turn": cancel_turn}, captured_auth=[])
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        cancelled = await sweeps.cancel_expired_turns(
            conn=conn, seam=_seam_client(app), settings=settings, now=lambda: NOW
        )

    assert cancelled == 1
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["handle"] == "ses-1"
    messages = threads_store.list_messages(conn, thread_id=expired.id)
    assert len(messages) == 1
    assert "still billed" in messages[0].text
    reloaded = threads_store.load_thread(
        conn, thread_id=expired.id, slug="acme", recipient_token="rcpt-1"
    )
    assert reloaded is not None
    assert reloaded.status == "running", "the thread stays running so the poller settles it"


# --------------------------------------------------------------------------
# archive_idle_threads
# --------------------------------------------------------------------------


async def test_archive_idle_threads_archives_only_past_the_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    old_thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="rcpt-old", title="old", now=NOW - timedelta(hours=48)
    )
    threads_store.bind_turn_boundary(
        conn,
        thread_id=old_thread.id,
        handle="ses-old",
        turn_event_id="evt-0",
        turn_started_at=NOW - timedelta(hours=48),
    )
    # idle_since only moves on create_thread/end_turn; simulate an old idle
    # thread directly rather than driving a full turn through it.
    with conn:
        conn.execute(
            "UPDATE threads SET idle_since = ? WHERE id = ?",
            (reports_store.dt_to_text(NOW - timedelta(hours=48)), old_thread.id),
        )
    recent_thread = threads_store.create_thread(
        conn,
        slug="acme",
        recipient_token="rcpt-recent",
        title="recent",
        now=NOW - timedelta(hours=1),
    )

    archive_calls: list[dict[str, object]] = []

    def archive_session(args: dict[str, object]) -> dict[str, object]:
        archive_calls.append(args)
        return {}

    app = build_fake_seam(behaviors={"archive_my_session": archive_session}, captured_auth=[])
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        archived = await sweeps.archive_idle_threads(
            conn=conn, seam=_seam_client(app), settings=settings, now=lambda: NOW
        )

    assert archived == 1
    assert len(archive_calls) == 1
    assert archive_calls[0]["handle"] == "ses-old"
    old_reloaded = threads_store.load_thread(
        conn, thread_id=old_thread.id, slug="acme", recipient_token="rcpt-old"
    )
    assert old_reloaded is not None
    assert old_reloaded.archived_at is not None
    recent_reloaded = threads_store.load_thread(
        conn, thread_id=recent_thread.id, slug="acme", recipient_token="rcpt-recent"
    )
    assert recent_reloaded is not None
    assert recent_reloaded.archived_at is None, "a recently idle thread must not be archived yet"


async def test_archive_idle_threads_skips_an_already_archived_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="rcpt-1", title="Q1", now=NOW - timedelta(hours=48)
    )
    with conn:
        conn.execute(
            "UPDATE threads SET idle_since = ? WHERE id = ?",
            (reports_store.dt_to_text(NOW - timedelta(hours=48)), thread.id),
        )
    threads_store.archive_thread(conn, thread_id=thread.id, now=NOW - timedelta(hours=2))

    app = build_fake_seam(behaviors={}, captured_auth=[])
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        archived = await sweeps.archive_idle_threads(
            conn=conn, seam=_seam_client(app), settings=settings, now=lambda: NOW
        )

    assert archived == 0, "an already-archived thread is not surfaced again"


async def test_archive_idle_threads_seam_failure_leaves_thread_unarchived_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    failing_thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="rcpt-fail", title="fail", now=NOW - timedelta(hours=48)
    )
    threads_store.bind_turn_boundary(
        conn,
        thread_id=failing_thread.id,
        handle="ses-fail",
        turn_event_id="evt-0",
        turn_started_at=NOW - timedelta(hours=48),
    )
    next_thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="rcpt-next", title="next", now=NOW - timedelta(hours=48)
    )
    threads_store.bind_turn_boundary(
        conn,
        thread_id=next_thread.id,
        handle="ses-next",
        turn_event_id="evt-0",
        turn_started_at=NOW - timedelta(hours=48),
    )
    with conn:
        conn.executemany(
            "UPDATE threads SET idle_since = ? WHERE id = ?",
            [
                (reports_store.dt_to_text(NOW - timedelta(hours=48)), failing_thread.id),
                (reports_store.dt_to_text(NOW - timedelta(hours=48)), next_thread.id),
            ],
        )

    def archive_session(args: dict[str, object]) -> dict[str, object]:
        if args["handle"] == "ses-fail":
            raise RuntimeError("seam is down")
        return {}

    app = build_fake_seam(behaviors={"archive_my_session": archive_session}, captured_auth=[])
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        archived = await sweeps.archive_idle_threads(
            conn=conn, seam=_seam_client(app), settings=settings, now=lambda: NOW
        )

    assert archived == 1, "the failing thread does not count, the next one does"
    failing_reloaded = threads_store.load_thread(
        conn, thread_id=failing_thread.id, slug="acme", recipient_token="rcpt-fail"
    )
    assert failing_reloaded is not None
    assert failing_reloaded.archived_at is None, (
        "a seam failure must never let the local archive happen anyway"
    )
    next_reloaded = threads_store.load_thread(
        conn, thread_id=next_thread.id, slug="acme", recipient_token="rcpt-next"
    )
    assert next_reloaded is not None
    assert next_reloaded.archived_at is not None, "one bad row must not stop the next thread"


# --------------------------------------------------------------------------
# prune_expired_recipients
# --------------------------------------------------------------------------


def test_prune_expired_recipients_removes_only_expired_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    reports_store.add_recipient(
        conn, slug="acme", name="Expired", label="reader", token="expired-1", now=NOW, ttl_days=-1
    )
    reports_store.add_recipient(
        conn, slug="acme", name="Live", label="reader", token="live-1", now=NOW, ttl_days=90
    )

    removed = sweeps.prune_expired_recipients(conn=conn, now=lambda: NOW)

    assert removed == 1
    remaining = reports_store.list_recipients(conn, slug="acme")
    assert [r.token for r in remaining] == ["live-1"]

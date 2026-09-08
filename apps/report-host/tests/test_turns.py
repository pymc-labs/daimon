"""Tests for report_host.turns — the pure decision, then the shell around it.

The shell tests drive `run_turn` against a real SQLite file in `tmp_path` and
an in-process fake seam (the shared `build_fake_seam` / `fake_seam_lifespan`
fixtures from `conftest.py`, the same harness plan 21-13 built for
`test_mcp_client.py`) — never a monkeypatch of `run_turn`'s own collaborators.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from report_host import reports_store, threads_store, turns
from report_host.config import Settings, load_settings
from report_host.mcp_client import SeamClient
from report_host.turns import read_turn_progress, run_turn
from starlette.types import Receive, Scope, Send

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

# Local aliases mirroring conftest's fixture types (see test_mcp_client.py for
# why these can't just be imported: `--import-mode=importlib` gives every test
# module its own namespace with no shared sys.path entry).
FakeASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
FakeSeamBuilder = Callable[..., FakeASGIApp]
FakeSeamLifespan = Callable[[FakeASGIApp], AbstractAsyncContextManager[None]]


# --------------------------------------------------------------------------
# Task 1: the pure half — read_turn_progress
# --------------------------------------------------------------------------


def _event(id_: str, type_: str, text: str | None = None) -> dict[str, object]:
    content: list[dict[str, object]] | None = None
    if text is not None:
        content = [{"type": "text", "text": text}]
    return {"id": id_, "type": type_, "content": content}


def test_read_turn_progress_with_only_boundary_event_is_not_done_and_has_no_text() -> None:
    events = [_event("evt-0", "agent.message", "should never appear")]
    result = read_turn_progress(
        status="running", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is False, "a turn with only its own boundary event is not finished"
    assert result.new_texts == (), "the boundary event's own text must never be replayed"


def test_read_turn_progress_returns_text_from_agent_message_after_boundary() -> None:
    events = [
        _event("evt-0", "agent.message", "boundary text"),
        _event("evt-1", "agent.message", "hello there"),
    ]
    result = read_turn_progress(
        status="running", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.new_texts == ("hello there",)
    assert result.is_done is False


def test_read_turn_progress_does_not_repeat_a_previously_seen_event() -> None:
    """The pin against duplicated answers: an id already reported is dropped."""
    events = [
        _event("evt-0", "agent.message", "boundary"),
        _event("evt-1", "agent.message", "hello"),
    ]
    result = read_turn_progress(
        status="running",
        events=events,
        turn_event_id="evt-0",
        seen_event_ids=frozenset({"evt-1"}),
    )
    assert result.new_texts == (), "an id already in seen_event_ids must not be replayed"


def test_read_turn_progress_is_done_when_idle_event_survives_the_filter() -> None:
    events = [_event("evt-0", "agent.message", "boundary"), _event("evt-2", "session.status_idle")]
    result = read_turn_progress(
        status="idle", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is True
    assert result.terminal_reason == "idle"


def test_read_turn_progress_bare_idle_status_with_no_idle_event_is_not_done() -> None:
    """The pin against the prototype's bug: a session that hasn't started yet
    reads idle right after the send, and that bare status must never be
    trusted without a surviving idle EVENT."""
    events = [_event("evt-0", "agent.message", "boundary")]
    result = read_turn_progress(
        status="idle", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is False, "a bare idle status with no idle event must not end the turn"


def test_read_turn_progress_rescheduling_status_is_not_done() -> None:
    result = read_turn_progress(
        status="rescheduling", events=[], turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is False, "rescheduling counts as still running"


def test_read_turn_progress_terminated_status_with_no_idle_event_is_done() -> None:
    result = read_turn_progress(
        status="terminated", events=[], turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is True
    assert result.terminal_reason == "terminated"


def test_read_turn_progress_idle_event_that_is_itself_the_boundary_is_not_done() -> None:
    events = [_event("evt-0", "session.status_idle")]
    result = read_turn_progress(
        status="idle", events=events, turn_event_id="evt-0", seen_event_ids=frozenset()
    )
    assert result.is_done is False, (
        "a boundary that is itself an idle event must not end the turn it starts"
    )


# --------------------------------------------------------------------------
# Task 3: the shell — run_turn against a fake seam and a real SQLite file
# --------------------------------------------------------------------------


def _settings(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "admin-secret")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://testserver/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "http://reports.example.com")
    settings = load_settings(_env_file=None)
    fields: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "poll_interval_seconds": 0.0,
        "reserve_usd": Decimal("0.60"),
        "turn_timeout_seconds": 1200,
    }
    fields.update(overrides)
    return settings.model_copy(update=fields)


def _setup(
    tmp_path: Path, *, cap_usd: Decimal = Decimal("100"), with_bundle: bool = True
) -> tuple[sqlite3.Connection, reports_store.ReportRow, threads_store.ThreadRow]:
    data_dir = tmp_path / "data"
    conn = reports_store.connect(data_dir)
    reports_store.save_report(
        conn,
        slug="acme",
        title="Acme Q3",
        tenant_id="tenant-1",
        agent_name="analyst",
        cap_usd=cap_usd,
        agent_token="seam-token-1",
        now=NOW,
    )
    if with_bundle:
        archive_dir = data_dir / "acme"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / "bundle.tar.gz"
        archive_path.write_bytes(b"bundle-bytes")
        reports_store.save_bundle_reference(
            conn,
            slug="acme",
            handle="bundle-handle-1",
            sha256="sha-1",
            expires_at=NOW + timedelta(days=90),
            archive_path=str(archive_path),
        )
        reports_store.set_current_pdf(conn, slug="acme", name="report.pdf")
    report = reports_store.load_report(conn, slug="acme")
    assert report is not None
    thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="rcpt-1", title="Q1", now=NOW
    )
    return conn, report, thread


def _begin(
    conn: sqlite3.Connection, thread: threads_store.ThreadRow, *, deadline_at: datetime
) -> threads_store.ThreadRow:
    ok = threads_store.begin_turn(
        conn, thread_id=thread.id, reserved_usd=Decimal("0.60"), deadline_at=deadline_at, now=NOW
    )
    assert ok, "begin_turn should succeed on a freshly created thread"
    reloaded = threads_store.load_thread(
        conn, thread_id=thread.id, slug=thread.slug, recipient_token=thread.recipient_token
    )
    assert reloaded is not None
    return reloaded


def _scripted(
    sequence: list[tuple[str, list[dict[str, object]]]],
) -> tuple[
    Callable[[dict[str, object]], dict[str, object]],
    Callable[[dict[str, object]], dict[str, object]],
]:
    """Step through ``sequence`` one (status, events) pair per poll iteration.

    ``get_my_session`` reads the current index; ``list_events`` reads the same
    index and then advances it — matching ``run_turn``'s own call order (status
    first, events second) so both calls in one iteration see the same entry.
    The last entry repeats if the loop polls more times than scripted.
    """
    state = {"i": 0}

    def get_my_session(_args: dict[str, object]) -> dict[str, object]:
        i = min(state["i"], len(sequence) - 1)
        status, _events = sequence[i]
        return {"status": status}

    def list_events(_args: dict[str, object]) -> dict[str, object]:
        i = min(state["i"], len(sequence) - 1)
        _status, events = sequence[i]
        state["i"] += 1
        return {"items": events, "next_page": None}

    return get_my_session, list_events


def _seam_client(app: FakeASGIApp) -> SeamClient:
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    return SeamClient(
        mcp_url="http://testserver/mcp",
        http_client=httpx.AsyncClient(transport=transport),
        transport=transport,
    )


async def test_run_turn_happy_path_streams_two_messages_then_settles_actual_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    get_my_session, list_events = _scripted(
        [
            ("running", [_event("evt-1", "agent.message", "first chunk")]),
            ("running", [_event("evt-2", "agent.message", "second chunk")]),
            ("idle", [_event("evt-3", "session.status_idle")]),
        ]
    )

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {
            "handle": "ses-1",
            "turn_event_id": "evt-0",
            "turn_started_at": "2026-09-08T12:00:00+00:00",
        }

    def get_turn_cost(_args: dict[str, object]) -> dict[str, object]:
        return {"cost_usd": "0.234500", "event_count": 4}

    app = build_fake_seam(
        behaviors={
            "start_turn": start_turn,
            "get_my_session": get_my_session,
            "list_events": list_events,
            "get_turn_cost": get_turn_cost,
        },
        captured_auth=[],
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="what does this mean?",
            now=lambda: NOW,
        )

    messages = threads_store.list_messages(conn, thread_id=thread.id)
    assistant_messages = [m for m in messages if m.role == "assistant"]
    assert [m.text for m in assistant_messages] == ["first chunk", "second chunk"]
    for m in assistant_messages:
        assert m.bundle_sha256 == "sha-1", "every assistant message must carry the bundle digest"
        assert m.pdf_revision == "report.pdf", "every assistant message must carry the PDF revision"

    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0.2345"), (
        "spend must equal the seam's real cost, not the 0.60 reserve"
    )

    updated_thread = threads_store.load_thread(
        conn, thread_id=thread.id, slug="acme", recipient_token="rcpt-1"
    )
    assert updated_thread is not None
    assert updated_thread.status == "idle"


async def test_run_turn_settles_actual_cost_on_terminated_status_with_no_idle_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    """The pin for D-06: reconciliation happens on ANY terminal, not only idle."""
    conn, _report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    get_my_session, list_events = _scripted([("running", []), ("terminated", [])])

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {
            "handle": "ses-1",
            "turn_event_id": "evt-0",
            "turn_started_at": "2026-09-08T12:00:00+00:00",
        }

    def get_turn_cost(_args: dict[str, object]) -> dict[str, object]:
        return {"cost_usd": "0.10", "event_count": 1}

    app = build_fake_seam(
        behaviors={
            "start_turn": start_turn,
            "get_my_session": get_my_session,
            "list_events": list_events,
            "get_turn_cost": get_turn_cost,
        },
        captured_auth=[],
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW,
        )

    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0.10"), (
        "a terminated turn (no idle event) must still be settled against its real cost"
    )
    updated_thread = threads_store.load_thread(
        conn, thread_id=thread.id, slug="acme", recipient_token="rcpt-1"
    )
    assert updated_thread is not None
    assert updated_thread.status == "idle"


async def test_run_turn_with_unpriced_turn_keeps_the_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    get_my_session, list_events = _scripted([("idle", [_event("evt-1", "session.status_idle")])])

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {
            "handle": "ses-1",
            "turn_event_id": "evt-0",
            "turn_started_at": "2026-09-08T12:00:00+00:00",
        }

    def get_turn_cost(_args: dict[str, object]) -> dict[str, object]:
        return {"cost_usd": None, "event_count": 1}

    app = build_fake_seam(
        behaviors={
            "start_turn": start_turn,
            "get_my_session": get_my_session,
            "list_events": list_events,
            "get_turn_cost": get_turn_cost,
        },
        captured_auth=[],
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW,
        )

    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0.60"), (
        "an unpriced turn (cost_usd null) must keep the reservation, never coerce to zero"
    )


async def test_run_turn_cancels_exactly_once_at_the_deadline_and_keeps_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    get_my_session, list_events = _scripted(
        [
            ("running", []),
            ("running", []),
            ("idle", [_event("evt-1", "session.status_idle")]),
        ]
    )

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {
            "handle": "ses-1",
            "turn_event_id": "evt-0",
            "turn_started_at": "2026-09-08T12:00:00+00:00",
        }

    def get_turn_cost(_args: dict[str, object]) -> dict[str, object]:
        return {"cost_usd": "0.30", "event_count": 2}

    cancel_calls: list[dict[str, object]] = []

    def cancel_turn(args: dict[str, object]) -> dict[str, object]:
        cancel_calls.append(args)
        return {"handle": args["handle"], "status": "running"}

    app = build_fake_seam(
        behaviors={
            "start_turn": start_turn,
            "get_my_session": get_my_session,
            "list_events": list_events,
            "get_turn_cost": get_turn_cost,
            "cancel_turn": cancel_turn,
        },
        captured_auth=[],
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    # now() is always past the deadline: every non-terminal iteration is
    # eligible to cancel, so a loop that cancelled on every iteration (rather
    # than once) would show up as more than one call.
    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW + timedelta(seconds=2000),
        )

    assert len(cancel_calls) == 1, "the deadline must cancel exactly once, not on every iteration"

    messages = threads_store.list_messages(conn, thread_id=thread.id)
    system_texts = [m.text for m in messages if m.role == "system"]
    assert len(system_texts) == 1
    assert "billed" in system_texts[0].lower(), (
        "the reader must be told a cancelled turn is still billed for what it consumed"
    )

    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0.30"), (
        "the deadline path must still settle against the real cost, not abandon the reserve"
    )


async def test_run_turn_at_the_cap_refuses_without_calling_the_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path, cap_usd=Decimal("0.10"))
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    start_calls: list[dict[str, object]] = []

    def start_turn(args: dict[str, object]) -> dict[str, object]:
        start_calls.append(args)
        return {
            "handle": "ses-1",
            "turn_event_id": "evt-0",
            "turn_started_at": "2026-09-08T12:00:00+00:00",
        }

    app = build_fake_seam(behaviors={"start_turn": start_turn}, captured_auth=[])
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW,
        )

    assert start_calls == [], "the seam must never be called once the cap is reached"
    messages = threads_store.list_messages(conn, thread_id=thread.id)
    assert any(m.role == "system" for m in messages), "the reader must be told the cap was hit"
    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0"), "a refused reservation must not touch spend"


async def test_run_turn_repushes_expired_bundle_once_and_retries_the_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    start_calls: list[dict[str, object]] = []

    def start_turn(args: dict[str, object]) -> dict[str, object]:
        start_calls.append(args)
        if len(start_calls) == 1:
            raise Exception("bundle expired; re-upload")  # noqa: TRY002
        return {
            "handle": "ses-1",
            "turn_event_id": "evt-0",
            "turn_started_at": "2026-09-08T12:00:00+00:00",
        }

    get_my_session, list_events = _scripted([("idle", [_event("evt-1", "session.status_idle")])])

    def get_turn_cost(_args: dict[str, object]) -> dict[str, object]:
        return {"cost_usd": "0.05", "event_count": 1}

    push_calls: list[bytes] = []

    def push_bundle_handler(body: bytes) -> dict[str, object]:
        return {
            "bundle": "bundle-handle-2",
            "sha256": "sha-2",
            "size_bytes": len(body),
            "expires_at": "2026-12-01T00:00:00+00:00",
        }

    app = build_fake_seam(
        behaviors={
            "start_turn": start_turn,
            "get_my_session": get_my_session,
            "list_events": list_events,
            "get_turn_cost": get_turn_cost,
        },
        captured_auth=[],
        push_bundle_handler=push_bundle_handler,
        captured_bundle_puts=push_calls,
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW,
        )

    assert len(start_calls) == 2, "exactly two send attempts: the failure and the retry"
    assert len(push_calls) == 1, "exactly one bundle re-push"
    assert push_calls[0] == b"bundle-bytes", "the re-push must send the archive's real bytes"

    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.bundle_handle == "bundle-handle-2"
    assert updated_report.spent_usd == Decimal("0.05")


async def test_run_turn_with_deleted_archive_stores_bundle_missing_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    """A real mutation of state (the file is deleted), not of code."""
    conn, report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))
    assert report.archive_path is not None
    Path(report.archive_path).unlink()

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        raise Exception("bundle expired; re-upload")  # noqa: TRY002

    push_calls: list[bytes] = []

    def push_bundle_handler(body: bytes) -> dict[str, object]:
        return {
            "bundle": "x",
            "sha256": "x",
            "size_bytes": 0,
            "expires_at": "2026-12-01T00:00:00+00:00",
        }

    app = build_fake_seam(
        behaviors={"start_turn": start_turn},
        captured_auth=[],
        push_bundle_handler=push_bundle_handler,
        captured_bundle_puts=push_calls,
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW,
        )

    assert push_calls == [], "a missing archive must never be re-pushed"
    messages = threads_store.list_messages(conn, thread_id=thread.id)
    system_texts = [m.text for m in messages if m.role == "system"]
    assert system_texts == [turns.BUNDLE_MISSING_MESSAGE]
    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0"), "the reservation must be released"


async def test_run_turn_with_null_archive_path_stores_bundle_missing_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path, with_bundle=False)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        raise Exception("bundle expired; re-upload")  # noqa: TRY002

    push_calls: list[bytes] = []

    def push_bundle_handler(body: bytes) -> dict[str, object]:
        return {
            "bundle": "x",
            "sha256": "x",
            "size_bytes": 0,
            "expires_at": "2026-12-01T00:00:00+00:00",
        }

    app = build_fake_seam(
        behaviors={"start_turn": start_turn},
        captured_auth=[],
        push_bundle_handler=push_bundle_handler,
        captured_bundle_puts=push_calls,
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW,
        )

    assert push_calls == [], "a null archive_path must never be re-pushed"
    messages = threads_store.list_messages(conn, thread_id=thread.id)
    system_texts = [m.text for m in messages if m.role == "system"]
    assert system_texts == [turns.BUNDLE_MISSING_MESSAGE]
    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0")


async def test_run_turn_bundle_expired_twice_gives_up_with_one_repush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    start_calls: list[dict[str, object]] = []

    def start_turn(args: dict[str, object]) -> dict[str, object]:
        start_calls.append(args)
        raise Exception("bundle expired; re-upload")  # noqa: TRY002

    push_calls: list[bytes] = []

    def push_bundle_handler(body: bytes) -> dict[str, object]:
        return {
            "bundle": "bundle-handle-2",
            "sha256": "sha-2",
            "size_bytes": len(body),
            "expires_at": "2026-12-01T00:00:00+00:00",
        }

    app = build_fake_seam(
        behaviors={"start_turn": start_turn},
        captured_auth=[],
        push_bundle_handler=push_bundle_handler,
        captured_bundle_puts=push_calls,
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW,
        )

    assert len(start_calls) == 2, "no third attempt after the retry also fails"
    assert len(push_calls) == 1, "exactly one re-push, not one per failure"
    messages = threads_store.list_messages(conn, thread_id=thread.id)
    system_texts = [m.text for m in messages if m.role == "system"]
    assert system_texts == [turns.BUNDLE_MISSING_MESSAGE]
    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0"), "the reservation must be settled either way"


async def test_run_turn_marks_report_unauthorized_on_401_and_makes_no_further_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {
            "handle": "ses-1",
            "turn_event_id": "evt-0",
            "turn_started_at": "2026-09-08T12:00:00+00:00",
        }

    app = build_fake_seam(
        behaviors={"start_turn": start_turn},
        captured_auth=[],
        unauthorized_tokens=frozenset({"seam-token-1"}),
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message="hello",
            now=lambda: NOW,
        )

    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.seam_status == "unauthorized"
    messages = threads_store.list_messages(conn, thread_id=thread.id)
    assert any(m.role == "system" for m in messages)
    assert updated_report.spent_usd == Decimal("0"), "the reservation must be released"


async def test_run_turn_reattach_sends_nothing_and_polls_straight_to_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn, _report, thread = _setup(tmp_path)
    thread = _begin(conn, thread, deadline_at=NOW + timedelta(seconds=1200))
    # Simulate a prior process already having reserved and sent before a
    # restart interrupted it: the reservation is already on the books and the
    # boundary is already bound, exactly what a restart-resume sweep sees.
    reserved = reports_store.reserve_budget(conn, slug="acme", amount=Decimal("0.60"))
    assert reserved is not None
    threads_store.bind_turn_boundary(
        conn, thread_id=thread.id, handle="ses-1", turn_event_id="evt-0", turn_started_at=NOW
    )
    reloaded = threads_store.load_thread(
        conn, thread_id=thread.id, slug=thread.slug, recipient_token=thread.recipient_token
    )
    assert reloaded is not None
    thread = reloaded

    start_calls: list[dict[str, object]] = []

    def start_turn(args: dict[str, object]) -> dict[str, object]:
        start_calls.append(args)
        return {
            "handle": "ses-1",
            "turn_event_id": "evt-0",
            "turn_started_at": "2026-09-08T12:00:00+00:00",
        }

    get_my_session, list_events = _scripted([("idle", [_event("evt-1", "session.status_idle")])])

    def get_turn_cost(_args: dict[str, object]) -> dict[str, object]:
        return {"cost_usd": "0.02", "event_count": 1}

    app = build_fake_seam(
        behaviors={
            "start_turn": start_turn,
            "get_my_session": get_my_session,
            "list_events": list_events,
            "get_turn_cost": get_turn_cost,
        },
        captured_auth=[],
    )
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async with fake_seam_lifespan(app):
        await run_turn(
            conn=conn,
            seam=_seam_client(app),
            settings=settings,
            thread=thread,
            message=None,
            now=lambda: NOW,
        )

    assert start_calls == [], "a re-attach must never call start_turn or continue_turn"
    updated_report = reports_store.load_report(conn, slug="acme")
    assert updated_report is not None
    assert updated_report.spent_usd == Decimal("0.02")

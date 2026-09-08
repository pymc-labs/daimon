"""Tests for the reader-facing routes.

Drives `build_reader_router` under `httpx.ASGITransport` with a real SQLite
file in `tmp_path`, a fake seam (the shared `build_fake_seam` fixture from
`conftest.py`), and a fake `run_turn` collaborator that only records its call
— `ask` is asserted to schedule exactly one turn without driving a real one
end to end.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from report_host import reports_store, threads_store
from report_host.config import Settings, load_settings
from report_host.mcp_client import SeamClient
from report_host.routes import build_reader_router
from starlette.types import Receive, Scope, Send

pytestmark = pytest.mark.asyncio

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


def _settings(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "admin-secret")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://testserver/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "http://reports.example.com")
    settings = load_settings(_env_file=None)
    fields: dict[str, object] = {"data_dir": tmp_path / "data"}
    fields.update(overrides)
    return settings.model_copy(update=fields)


def _poisoned_seam() -> SeamClient:
    """A SeamClient that fails loudly if any test in this file actually calls it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected seam call: {request.url}")

    transport = httpx.MockTransport(handler)
    return SeamClient(
        mcp_url="http://poisoned/mcp", http_client=httpx.AsyncClient(transport=transport)
    )


def _seam_client(app: FakeASGIApp) -> SeamClient:
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    return SeamClient(
        mcp_url="http://testserver/mcp",
        http_client=httpx.AsyncClient(transport=transport),
        transport=transport,
    )


def _seed_report(
    conn: sqlite3.Connection,
    *,
    slug: str = "acme",
    cap_usd: Decimal = Decimal("10"),
    tenant_id: str = "tenant-secret-1",
    agent_token: str = "seam-token-secret-1",
) -> reports_store.ReportRow:
    reports_store.save_report(
        conn,
        slug=slug,
        title="Acme Q3",
        tenant_id=tenant_id,
        agent_name="analyst",
        cap_usd=cap_usd,
        agent_token=agent_token,
        now=NOW,
    )
    report = reports_store.load_report(conn, slug=slug)
    assert report is not None
    return report


def _seed_recipient(
    conn: sqlite3.Connection,
    *,
    slug: str = "acme",
    token: str = "tok-1",
    name: str = "Jane",
    ttl_days: int = 90,
) -> reports_store.RecipientRow:
    return reports_store.add_recipient(
        conn, slug=slug, name=name, label="reader", token=token, now=NOW, ttl_days=ttl_days
    )


def _make_app(
    *,
    settings: Settings,
    conn: sqlite3.Connection,
    seam: SeamClient,
    run_turn: Callable[..., Awaitable[None]] | None = None,
) -> FastAPI:
    app = FastAPI()
    kwargs: dict[str, Any] = {
        "settings": settings,
        "conn_factory": lambda: conn,
        "seam": seam,
        "now": lambda: NOW,
    }
    if run_turn is not None:
        kwargs["run_turn"] = run_turn
    app.include_router(build_reader_router(**kwargs))
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


# --------------------------------------------------------------------------
# Recipient resolution
# --------------------------------------------------------------------------


async def test_viewer_returns_403_with_no_token_and_no_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp = await client.get("/r/acme")
    assert resp.status_code == 403


async def test_viewer_returns_403_for_a_token_belonging_to_a_different_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn, slug="acme")
    _seed_report(conn, slug="other")
    _seed_recipient(conn, slug="other", token="tok-other")
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp = await client.get("/r/acme?k=tok-other")
    assert resp.status_code == 403


async def test_expired_recipient_returns_403_with_same_message_as_unknown_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-expired", ttl_days=-1)
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        unknown_resp = await client.get("/r/acme?k=does-not-exist")
        expired_resp = await client.get("/r/acme?k=tok-expired")
    assert unknown_resp.status_code == 403
    assert expired_resp.status_code == 403
    assert unknown_resp.json()["detail"] == expired_resp.json()["detail"], (
        "unknown and expired tokens must be indistinguishable to the caller"
    )


async def test_viewer_sets_cookie_httponly_secure_samesite_lax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp = await client.get("/r/acme?k=tok-1")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "rh_acme=tok-1" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "secure" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


# --------------------------------------------------------------------------
# State and thread reads
# --------------------------------------------------------------------------


async def test_state_returns_only_this_recipients_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-a", name="Alice")
    _seed_recipient(conn, token="tok-b", name="Bob")
    threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-a", title="Alice's Q", now=NOW
    )
    threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-b", title="Bob's Q", now=NOW
    )
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp_a = await client.get("/api/acme/state?k=tok-a")
        resp_b = await client.get("/api/acme/state?k=tok-b")
    assert len(resp_a.json()["threads"]) == 1
    assert resp_a.json()["threads"][0]["title"] == "Alice's Q"
    assert len(resp_b.json()["threads"]) == 1
    assert resp_b.json()["threads"][0]["title"] == "Bob's Q"


async def test_state_payload_excludes_seam_token_bundle_handle_and_tenant_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn, tenant_id="tenant-secret-1", agent_token="seam-token-secret-1")
    reports_store.save_bundle_reference(
        conn,
        slug="acme",
        handle="bundle-handle-secret-1",
        sha256="sha-1",
        expires_at=NOW + timedelta(days=90),
        archive_path=str(tmp_path / "bundle.tar.gz"),
    )
    _seed_recipient(conn, token="tok-1")
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp = await client.get("/api/acme/state?k=tok-1")
    assert "tenant-secret-1" not in resp.text
    assert "seam-token-secret-1" not in resp.text
    assert "bundle-handle-secret-1" not in resp.text


async def test_thread_belonging_to_another_recipient_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-a")
    _seed_recipient(conn, token="tok-b")
    thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-a", title="Q", now=NOW
    )
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp = await client.get(f"/api/acme/threads/{thread.id}?k=tok-b")
    assert resp.status_code == 404


async def test_thread_messages_after_watermark_excludes_earlier_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-1", title="Q", now=NOW
    )
    m1 = threads_store.add_message(
        conn,
        thread_id=thread.id,
        role="user",
        text="first",
        now=NOW,
        bundle_sha256=None,
        pdf_revision=None,
    )
    threads_store.add_message(
        conn,
        thread_id=thread.id,
        role="assistant",
        text="second",
        now=NOW,
        bundle_sha256=None,
        pdf_revision=None,
    )
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp = await client.get(f"/api/acme/threads/{thread.id}?k=tok-1&after={m1.id}")
    texts = [m["text"] for m in resp.json()["messages"]]
    assert texts == ["second"]


# --------------------------------------------------------------------------
# ask
# --------------------------------------------------------------------------


async def test_ask_at_running_turns_cap_returns_429_and_schedules_no_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch, max_running_turns_per_report=1)
    other = threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-1", title="running", now=NOW
    )
    started = threads_store.begin_turn(
        conn,
        thread_id=other.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=60),
        now=NOW,
    )
    assert started
    recorder = _RunTurnRecorder()
    app = _make_app(settings=settings, conn=conn, seam=_poisoned_seam(), run_turn=recorder)
    async with await _client(app) as client:
        resp = await client.post("/api/acme/ask?k=tok-1", json={"message": "hello"})
    assert resp.status_code == 429
    assert recorder.calls == []


async def test_ask_at_open_thread_cap_on_new_thread_returns_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    settings = _settings(
        tmp_path=tmp_path, monkeypatch=monkeypatch, max_open_threads_per_recipient=1
    )
    threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-1", title="existing", now=NOW
    )
    recorder = _RunTurnRecorder()
    app = _make_app(settings=settings, conn=conn, seam=_poisoned_seam(), run_turn=recorder)
    async with await _client(app) as client:
        resp = await client.post("/api/acme/ask?k=tok-1", json={"message": "a new question"})
    assert resp.status_code == 429
    assert recorder.calls == []


async def test_ask_followup_in_existing_thread_still_allowed_at_open_thread_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    settings = _settings(
        tmp_path=tmp_path, monkeypatch=monkeypatch, max_open_threads_per_recipient=1
    )
    thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-1", title="existing", now=NOW
    )
    recorder = _RunTurnRecorder()
    app = _make_app(settings=settings, conn=conn, seam=_poisoned_seam(), run_turn=recorder)
    async with await _client(app) as client:
        resp = await client.post(
            "/api/acme/ask?k=tok-1", json={"message": "a follow-up", "thread": thread.id}
        )
    assert resp.status_code == 200
    assert len(recorder.calls) == 1


async def test_ask_on_unauthorized_report_is_refused_without_a_seam_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    reports_store.set_seam_status(conn, slug="acme", status="unauthorized")
    _seed_recipient(conn, token="tok-1")
    recorder = _RunTurnRecorder()
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
        run_turn=recorder,
    )
    async with await _client(app) as client:
        resp = await client.post("/api/acme/ask?k=tok-1", json={"message": "hello"})
    assert resp.status_code == 409
    assert recorder.calls == []


async def test_ask_schedules_exactly_one_turn_and_stores_readers_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1", name="Jane")
    recorder = _RunTurnRecorder()
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
        run_turn=recorder,
    )
    async with await _client(app) as client:
        resp = await client.post("/api/acme/ask?k=tok-1", json={"message": "why did revenue drop?"})
    assert resp.status_code == 200
    thread_id = resp.json()["thread"]
    assert len(recorder.calls) == 1
    scheduled_thread = recorder.calls[0]["thread"]
    assert isinstance(scheduled_thread, threads_store.ThreadRow)
    assert scheduled_thread.id == thread_id
    messages = threads_store.list_messages(conn, thread_id=thread_id)
    user_messages = [m for m in messages if m.role == "user"]
    assert [m.text for m in user_messages] == ["why did revenue drop?"], (
        "the stored message is the reader's raw question, not the seam preamble"
    )


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------


async def test_cancel_on_running_thread_calls_seam_once_and_stores_billing_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-1", title="Q", now=NOW
    )
    threads_store.begin_turn(
        conn,
        thread_id=thread.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=60),
        now=NOW,
    )
    threads_store.bind_turn_boundary(
        conn, thread_id=thread.id, handle="ses-1", turn_event_id="evt-0", turn_started_at=NOW
    )

    calls: list[dict[str, object]] = []

    def cancel_turn(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {"status": "terminated"}

    fake_app = build_fake_seam(behaviors={"cancel_turn": cancel_turn}, captured_auth=[])
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_seam_client(fake_app),
    )
    async with fake_seam_lifespan(fake_app), await _client(app) as client:
        resp = await client.post(f"/api/acme/threads/{thread.id}/cancel?k=tok-1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "terminated"
    assert len(calls) == 1
    messages = threads_store.list_messages(conn, thread_id=thread.id)
    system_texts = [m.text for m in messages if m.role == "system"]
    assert len(system_texts) == 1
    assert "billed" in system_texts[0].lower()


async def test_cancel_on_idle_thread_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-1", title="Q", now=NOW
    )
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp = await client.post(f"/api/acme/threads/{thread.id}/cancel?k=tok-1")
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# close
# --------------------------------------------------------------------------


async def test_close_archives_thread_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    thread = threads_store.create_thread(
        conn, slug="acme", recipient_token="tok-1", title="Q", now=NOW
    )
    threads_store.begin_turn(
        conn,
        thread_id=thread.id,
        reserved_usd=Decimal("0.60"),
        deadline_at=NOW + timedelta(seconds=60),
        now=NOW,
    )
    threads_store.bind_turn_boundary(
        conn, thread_id=thread.id, handle="ses-1", turn_event_id="evt-0", turn_started_at=NOW
    )
    threads_store.end_turn(conn, thread_id=thread.id, now=NOW)

    calls: list[dict[str, object]] = []

    def archive_my_session(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {}

    fake_app = build_fake_seam(
        behaviors={"archive_my_session": archive_my_session}, captured_auth=[]
    )
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_seam_client(fake_app),
    )
    async with fake_seam_lifespan(fake_app), await _client(app) as client:
        first = await client.post(f"/api/acme/threads/{thread.id}/close?k=tok-1")
        second = await client.post(f"/api/acme/threads/{thread.id}/close?k=tok-1")
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(calls) == 1, "closing an already-archived thread must not call the seam again"
    reloaded = threads_store.load_thread(
        conn, thread_id=thread.id, slug="acme", recipient_token="tok-1"
    )
    assert reloaded is not None
    assert reloaded.archived_at is not None


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


async def test_file_route_rejects_a_dot_dot_segment_before_any_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``..``-carrying name that still routes as one segment (Starlette's own
    router already collapses a bare ``..`` path segment before it reaches any
    handler — this pins the defense-in-depth check for a name that survives
    routing intact, e.g. an embedded ``..`` next to legitimate-looking text)."""
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-top-secret")
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        resp = await client.get("/files/acme/..secret.pdf?k=tok-1")
    assert resp.status_code == 400
    assert secret.read_bytes() != b"", "the real file on disk must be untouched"


async def test_file_route_rejects_name_with_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = reports_store.connect(tmp_path / "data")
    _seed_report(conn)
    _seed_recipient(conn, token="tok-1")
    app = _make_app(
        settings=_settings(tmp_path=tmp_path, monkeypatch=monkeypatch),
        conn=conn,
        seam=_poisoned_seam(),
    )
    async with await _client(app) as client:
        # A backslash is not a URL path separator, so this still routes as one
        # segment and reaches the handler with the separator intact.
        resp = await client.get("/files/acme/sub%5Cfile.pdf?k=tok-1")
    assert resp.status_code == 400

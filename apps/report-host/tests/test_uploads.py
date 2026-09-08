"""Tests for the two upload routes: publish archive and per-turn revised PDF.

Drives `build_uploads_router` under `httpx.ASGITransport` with a real SQLite
file in `tmp_path`, a fake seam (an `httpx.MockTransport` behind `SeamClient`
— `push_bundle` is a plain HTTP PUT, so no MCP wire protocol is needed here),
and hand-built `tarfile` archives, including the hostile ones, which is the
only way to prove the extraction guards. Never imports daimon: capability
tokens are minted inline, the same way `test_capability.py` does, using the
identical HMAC wire format `report_host.capability.verify_token` checks.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import sqlite3
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from report_host import reports_store, threads_store
from report_host.config import Settings, load_settings
from report_host.mcp_client import SeamClient
from report_host.uploads import (
    _PUBLISH_INVALID_MESSAGE,  # pyright: ignore[reportPrivateUsage]
    _UPLOAD_INVALID_MESSAGE,  # pyright: ignore[reportPrivateUsage]
    build_uploads_router,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
ADMIN_SECRET = "admin-secret"
REPORT_PDF_BYTES = b"%PDF-1.4 fake report content for the archive root"


# --------------------------------------------------------------------------
# Settings / app / seam fixtures
# --------------------------------------------------------------------------


def _settings(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", ADMIN_SECRET)
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://testserver/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "http://reports.example.com")
    settings = load_settings(_env_file=None)
    fields: dict[str, object] = {"data_dir": tmp_path / "data"}
    fields.update(overrides)
    return settings.model_copy(update=fields)


SeamHandler = Callable[[httpx.Request], httpx.Response]


def _seam(handler: SeamHandler | None = None, *, requests: list[httpx.Request]) -> SeamClient:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "bundle": "bundle-handle-1",
                "sha256": "a" * 64,
                "size_bytes": 5,
                "expires_at": "2026-12-01T00:00:00Z",
            },
        )

    active_handler = handler if handler is not None else default_handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return active_handler(request)

    transport = httpx.MockTransport(recording_handler)
    return SeamClient(
        mcp_url="http://testserver/mcp", http_client=httpx.AsyncClient(transport=transport)
    )


def _make_app(*, settings: Settings, conn: sqlite3.Connection, seam: SeamClient) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_uploads_router(settings=settings, conn_factory=lambda: conn, seam=seam)
    )
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


# --------------------------------------------------------------------------
# Store seeding helpers
# --------------------------------------------------------------------------


def _seed_report(
    conn: sqlite3.Connection,
    *,
    slug: str = "acme",
    cap_usd: Decimal = Decimal("10"),
    tenant_id: str = "tenant-1",
    agent_token: str = "seam-token-secret-1",
) -> reports_store.ReportRow:
    return reports_store.save_report(
        conn,
        slug=slug,
        title="Acme Q3",
        tenant_id=tenant_id,
        agent_name="analyst",
        cap_usd=cap_usd,
        agent_token=agent_token,
        now=NOW,
    )


def _seed_recipient(conn: sqlite3.Connection, *, slug: str = "acme", name: str = "Jane") -> None:
    reports_store.add_recipient(
        conn,
        slug=slug,
        name=name,
        label="reader",
        token=f"tok-{name}",
        now=NOW,
        ttl_days=90,
    )


def _seed_running_thread(
    conn: sqlite3.Connection, *, slug: str = "acme", recipient_token: str = "rtok"
) -> threads_store.ThreadRow:
    thread = threads_store.create_thread(
        conn, slug=slug, recipient_token=recipient_token, title="a question", now=NOW
    )
    began = threads_store.begin_turn(
        conn,
        thread_id=thread.id,
        reserved_usd=Decimal("0.5"),
        deadline_at=NOW + timedelta(seconds=60),
        now=NOW,
    )
    assert began
    loaded = threads_store.load_thread(
        conn, thread_id=thread.id, slug=slug, recipient_token=recipient_token
    )
    assert loaded is not None
    return loaded


# --------------------------------------------------------------------------
# Capability-token minting — inlined, mirrors test_capability.py exactly
# --------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _mint(secret: str, payload: dict[str, object]) -> str:
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def _capability_payload(**over: object) -> dict[str, object]:
    # `exp` is computed off the REAL clock, not the fixed `NOW` used for store
    # timestamps: `uploads.py`'s handlers call `datetime.now(UTC)` directly
    # (no clock injection, per this plan's own router signature), so a token
    # must be valid against whatever moment the test actually runs.
    base: dict[str, object] = {
        "slug": "acme",
        "op": "report",
        "name": None,
        "max_bytes": 10_000_000,
        "exp": int(datetime.now(UTC).timestamp()) + 300,
        "jti": "jti-1",
    }
    base.update(over)
    return base


def _capability_token(secret: str = ADMIN_SECRET, **over: object) -> str:
    return _mint(secret, _capability_payload(**over))


# --------------------------------------------------------------------------
# Archive-building helpers — real tarfile archives, including hostile ones
# --------------------------------------------------------------------------


def _build_archive(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def _pdf_member(
    data: bytes = REPORT_PDF_BYTES, name: str = "report.pdf"
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    return info, data


def _valid_archive(pdf_bytes: bytes = REPORT_PDF_BYTES) -> bytes:
    other_data = b"a,b,c\n1,2,3\n"
    other = tarfile.TarInfo(name="bundle/data.csv")
    other.size = len(other_data)
    return _build_archive([_pdf_member(pdf_bytes), (other, other_data)])


def _archive_with_absolute_path_member() -> bytes:
    evil_data = b"evil"
    evil = tarfile.TarInfo(name="/etc/passwd")
    evil.size = len(evil_data)
    return _build_archive([_pdf_member(), (evil, evil_data)])


def _archive_with_traversal_member() -> bytes:
    evil_data = b"evil"
    evil = tarfile.TarInfo(name="../evil.txt")
    evil.size = len(evil_data)
    return _build_archive([_pdf_member(), (evil, evil_data)])


def _archive_with_symlink_member() -> bytes:
    evil = tarfile.TarInfo(name="link")
    evil.type = tarfile.SYMTYPE
    evil.linkname = "/etc/passwd"
    return _build_archive([_pdf_member(), (evil, None)])


def _archive_with_no_report_pdf() -> bytes:
    data = b"not the report"
    other = tarfile.TarInfo(name="other.pdf")
    other.size = len(data)
    return _build_archive([(other, data)])


def _bomb_archive(*, declared_size: int) -> bytes:
    """A member whose declared size exceeds a ceiling but whose real bytes are small.

    Real content is `declared_size` zero bytes — genuinely that large before
    compression, but gzip compresses a run of zeros to almost nothing, which
    is exactly the shape of a real compression bomb: a tiny file on the wire
    that declares (and, if extracted naively, produces) a huge payload.
    """
    return _build_archive([_pdf_member(b"\x00" * declared_size)])


# --------------------------------------------------------------------------
# Publish route: invalid / replayed / missing-report capability tokens
# --------------------------------------------------------------------------


async def test_publish_with_garbage_token_returns_404_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    requests: list[httpx.Request] = []
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=requests))
    async with await _client(app) as client:
        resp = await client.put("/publish/not-a-real-token", content=_valid_archive())
    assert resp.status_code == 404
    assert resp.json()["detail"] == _PUBLISH_INVALID_MESSAGE
    assert not (settings.data_dir / "acme" / "bundle.tar.gz").exists()
    assert requests == []


async def test_publish_with_tampered_payload_returns_same_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    good = _capability_token()
    payload_b64, sig_b64 = good.split(".", 1)
    tampered = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    tampered["slug"] = "victim"
    forged = f"{_b64(json.dumps(tampered, separators=(',', ':')).encode())}.{sig_b64}"
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{forged}", content=_valid_archive())
    assert resp.status_code == 404
    assert resp.json()["detail"] == _PUBLISH_INVALID_MESSAGE


async def test_publish_with_expired_token_returns_same_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    expired = _capability_token(exp=1)  # 1970-01-01T00:00:01Z — expired regardless of wall clock
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{expired}", content=_valid_archive())
    assert resp.status_code == 404
    assert resp.json()["detail"] == _PUBLISH_INVALID_MESSAGE


async def test_publish_with_wrong_op_returns_same_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token(op="blog")
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=_valid_archive())
    assert resp.status_code == 404
    assert resp.json()["detail"] == _PUBLISH_INVALID_MESSAGE


async def test_publish_token_cannot_be_replayed_and_seam_called_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token(jti="replay-me")
    requests: list[httpx.Request] = []
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=requests))
    async with await _client(app) as client:
        first = await client.put(f"/publish/{token}", content=_valid_archive())
        second = await client.put(f"/publish/{token}", content=_valid_archive())
    assert first.status_code == 200
    assert second.status_code == 404
    assert second.json()["detail"] == _PUBLISH_INVALID_MESSAGE
    assert len(requests) == 1, "the seam must be called exactly once across both attempts"


async def test_publish_burns_token_before_reading_body_even_when_the_body_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves burn-before-read, not just burn-before-push.

    The first attempt's body is oversized and never gets past `_spool_body`
    (413) — under the correct ordering the token is ALREADY burnt by then, so
    a second, otherwise-valid attempt with the same token is refused before
    the seam is ever reached. Mutation-sensitive: moving the burn to after
    `_spool_body` lets the first (rejected) attempt leave the token unburnt,
    and the second attempt then succeeds — `requests` goes from `[]` to
    having one entry. Confirmed live: reverting `uploads.py`'s burn call to
    sit after `_spool_body` turns this test red (`requests == [<PUT /bundles>]`
    instead of `[]`); reverting the edit turns it green again.
    """
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    valid_archive = _valid_archive()
    token = _capability_token(jti="oversized-first", max_bytes=len(valid_archive) + 50)
    requests: list[httpx.Request] = []
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=requests))
    async with await _client(app) as client:
        first = await client.put(f"/publish/{token}", content=b"x" * (len(valid_archive) + 1000))
        second = await client.put(f"/publish/{token}", content=valid_archive)
    assert first.status_code == 413
    assert second.status_code == 404
    assert second.json()["detail"] == _PUBLISH_INVALID_MESSAGE
    assert requests == [], (
        "the oversized first attempt must already have burnt the token, so the "
        "second attempt (and the seam) is never reached"
    )


async def test_publish_with_capability_for_missing_report_returns_same_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    # No report seeded for "ghost".
    token = _capability_token(slug="ghost")
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=_valid_archive())
    assert resp.status_code == 404
    assert resp.json()["detail"] == _PUBLISH_INVALID_MESSAGE


# --------------------------------------------------------------------------
# Publish route: size cap and content-type checks
# --------------------------------------------------------------------------


async def test_publish_body_over_the_smaller_cap_returns_413_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token(max_bytes=10)
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=b"x" * 20)
    assert resp.status_code == 413
    assert not (settings.data_dir / "acme").exists()


async def test_publish_non_gzip_body_returns_415(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token()
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=b"not a gzip archive at all")
    assert resp.status_code == 415


# --------------------------------------------------------------------------
# Publish route: hostile-archive rejection
# --------------------------------------------------------------------------


async def test_publish_archive_without_report_pdf_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token()
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=_archive_with_no_report_pdf())
    assert resp.status_code == 422
    assert "report.pdf" in resp.json()["detail"]


async def test_publish_archive_with_absolute_path_member_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token()
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=_archive_with_absolute_path_member())
    assert resp.status_code == 422
    assert not (settings.data_dir / "acme").exists(), (
        "a refused archive must not create the report's own directory at all"
    )
    assert not (settings.data_dir / "etc" / "passwd").exists()
    assert not (settings.data_dir / "passwd").exists()


def _snapshot_ignoring_store_infra(data_dir: Path) -> set[str]:
    """The report directory's parent's contents, minus the host's own SQLite/registry files.

    `reports_store.connect()` and the capability jti burn both write into
    `data_dir` as ordinary, expected side effects of handling any request at
    all (the WAL/registry files, not anything the archive controls) — a
    literal before/after diff would flag those every time, unrelated to
    whether the traversal member escaped anywhere. Filtered out by name so
    the comparison is only sensitive to what the archive itself could cause.
    """
    if not data_dir.exists():
        return set()
    ignored_prefixes = ("host.sqlite", "consumed.json")
    return {
        str(p.relative_to(data_dir))
        for p in data_dir.rglob("*")
        if not p.name.startswith(ignored_prefixes)
    }


async def test_publish_archive_with_traversal_member_is_refused_and_leaves_parent_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    before = _snapshot_ignoring_store_infra(settings.data_dir)
    token = _capability_token()
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=_archive_with_traversal_member())
    assert resp.status_code == 422
    after = _snapshot_ignoring_store_infra(settings.data_dir)
    assert before == after, "a refused traversal archive must not change the parent directory"
    assert not (settings.data_dir / "evil.txt").exists()


async def test_publish_archive_with_symlink_member_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token()
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=_archive_with_symlink_member())
    assert resp.status_code == 422


async def test_publish_compression_bomb_is_refused_without_exhausting_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A small decompressed ceiling keeps this test's own archive tiny while
    # still exercising the exact declared-size-over-ceiling refusal path.
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch, max_pdf_bytes=1_000)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token()
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    archive = _bomb_archive(declared_size=2_000)
    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=archive)
    assert resp.status_code == 422
    assert "ceiling" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Publish route: happy path, re-publish, and seam failure handling
# --------------------------------------------------------------------------


async def test_publish_happy_path_writes_pdf_archive_and_returns_recipient_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn, agent_token="seam-token-secret-1")
    _seed_recipient(conn, name="Jane")
    _seed_recipient(conn, name="Bob")
    token = _capability_token()
    requests: list[httpx.Request] = []
    archive = _valid_archive()
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=requests))

    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=archive)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["slug"] == "acme"
    links = payload["links"]
    assert len(links) == 2
    for link in links:
        assert link["link"].startswith("http://reports.example.com/r/acme?k=")

    report = reports_store.load_report(conn, slug="acme")
    assert report is not None
    assert report.current_pdf == "v1.pdf"
    assert (settings.data_dir / "acme" / "v1.pdf").read_bytes() == REPORT_PDF_BYTES

    archive_path = settings.data_dir / "acme" / "bundle.tar.gz"
    assert archive_path.read_bytes() == archive
    assert report.archive_path == str(archive_path)
    assert report.bundle_handle == "bundle-handle-1"

    assert len(requests) == 1, "push_bundle must be called exactly once"
    assert requests[0].headers["authorization"] == "Bearer seam-token-secret-1", (
        "the archive must be pushed under the report's own seam token, not a shared one"
    )


async def test_second_publish_of_same_slug_replaces_archive_bytes_with_no_leftover_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    first_archive = _valid_archive(b"%PDF first revision bytes")
    second_archive = _valid_archive(b"%PDF second, different revision bytes")
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))

    async with await _client(app) as client:
        first = await client.put(
            f"/publish/{_capability_token(jti='pub-1')}", content=first_archive
        )
        second = await client.put(
            f"/publish/{_capability_token(jti='pub-2')}", content=second_archive
        )
    assert first.status_code == 200
    assert second.status_code == 200

    archive_path = settings.data_dir / "acme" / "bundle.tar.gz"
    assert archive_path.read_bytes() == second_archive, "the second publish's bytes must win"

    leftovers = [p for p in (settings.data_dir / "acme").iterdir() if p.name.startswith(".")]
    assert leftovers == [], "no temporary file should survive a completed publish"


async def test_publish_archive_survives_a_seam_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token()

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "seam is down"})

    requests: list[httpx.Request] = []
    app = _make_app(settings=settings, conn=conn, seam=_seam(failing_handler, requests=requests))
    archive = _valid_archive()

    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=archive)

    assert resp.status_code == 502
    archive_path = settings.data_dir / "acme" / "bundle.tar.gz"
    assert archive_path.exists(), "a failed push must not take the only copy of the archive with it"
    assert archive_path.read_bytes() == archive


async def test_publish_seam_unauthorized_marks_report_and_asks_for_republish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    token = _capability_token()

    def unauthorized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    app = _make_app(settings=settings, conn=conn, seam=_seam(unauthorized_handler, requests=[]))

    async with await _client(app) as client:
        resp = await client.put(f"/publish/{token}", content=_valid_archive())

    assert resp.status_code == 409
    assert "re-publish" in resp.json()["detail"]
    report = reports_store.load_report(conn, slug="acme")
    assert report is not None
    assert report.seam_status == "unauthorized"


# --------------------------------------------------------------------------
# Turn upload route
# --------------------------------------------------------------------------


async def test_turn_upload_with_unknown_token_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put("/upload/no-such-token", content=b"%PDF anything")
    assert resp.status_code == 404
    assert resp.json()["detail"] == _UPLOAD_INVALID_MESSAGE


async def test_turn_upload_for_an_ended_turn_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    thread = _seed_running_thread(conn)
    threads_store.end_turn(conn, thread_id=thread.id, now=NOW)
    reports_store.create_upload_token(
        conn, token="stale-tok", slug="acme", thread_id=thread.id, now=NOW
    )
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put("/upload/stale-tok", content=b"%PDF anything")
    assert resp.status_code == 404
    assert resp.json()["detail"] == _UPLOAD_INVALID_MESSAGE


async def test_turn_upload_non_pdf_body_returns_415(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    thread = _seed_running_thread(conn)
    reports_store.create_upload_token(
        conn, token="tok-1", slug="acme", thread_id=thread.id, now=NOW
    )
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put("/upload/tok-1", content=b"not a pdf")
    assert resp.status_code == 415


async def test_turn_upload_over_max_bytes_returns_413_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch, max_pdf_bytes=10)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    thread = _seed_running_thread(conn)
    reports_store.create_upload_token(
        conn, token="tok-1", slug="acme", thread_id=thread.id, now=NOW
    )
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    async with await _client(app) as client:
        resp = await client.put("/upload/tok-1", content=b"%PDF" + b"x" * 20)
    assert resp.status_code == 413
    assert not (settings.data_dir / "acme").exists()


async def test_turn_upload_happy_path_records_revision_and_cannot_be_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path=tmp_path, monkeypatch=monkeypatch)
    conn = reports_store.connect(settings.data_dir)
    _seed_report(conn)
    thread = _seed_running_thread(conn)
    reports_store.create_upload_token(
        conn, token="tok-1", slug="acme", thread_id=thread.id, now=NOW
    )
    app = _make_app(settings=settings, conn=conn, seam=_seam(requests=[]))
    pdf_bytes = b"%PDF revised report bytes"

    async with await _client(app) as client:
        first = await client.put("/upload/tok-1", content=pdf_bytes)
        replay = await client.put("/upload/tok-1", content=pdf_bytes)

    assert first.status_code == 200
    assert first.json()["shown_as"] == "v1.pdf"
    assert (settings.data_dir / "acme" / "v1.pdf").read_bytes() == pdf_bytes

    report = reports_store.load_report(conn, slug="acme")
    assert report is not None
    assert report.current_pdf == "v1.pdf"
    revisions = reports_store.list_revisions(conn, slug="acme")
    assert len(revisions) == 1
    assert revisions[0].by_thread == str(thread.id)

    assert replay.status_code == 404, "the same per-turn token must not be usable twice"
    assert replay.json()["detail"] == _UPLOAD_INVALID_MESSAGE

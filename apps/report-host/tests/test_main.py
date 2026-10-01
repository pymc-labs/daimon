"""Tests for report_host.main — the app factory and its lifespan.

Drives `create_app`'s assembled `FastAPI` app the same way every other
report-host router test does: `httpx.ASGITransport` inside the test's own
event loop, plus the shared `fake_seam_lifespan` fixture (generic over any
ASGI app, not just the fake seam) to run the real lifespan's startup and
shutdown. `main.py` opens its own `sqlite3.Connection` synchronously inside
`create_app`, which `fastapi.testclient.TestClient` cannot drive — it runs
the ASGI app in a separate portal thread, and sqlite3 connections are
thread-bound; production never hits this because `uvicorn.run` shares the
one thread `create_app` ran in.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import httpx
import pytest
from report_host import main as main_mod
from report_host.config import Settings, load_settings
from starlette.types import Receive, Scope, Send

# Local alias mirroring conftest's fixture type (see test_mcp_client.py for
# why this can't just be imported: `--import-mode=importlib` gives every
# test module its own namespace with no shared sys.path entry).
FakeASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
FakeSeamLifespan = Callable[[FakeASGIApp], AbstractAsyncContextManager[None]]


def _settings(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    monkeypatch.setenv("DAIMON_REPORT__ADMIN_SECRETS", "admin-secret")
    monkeypatch.setenv("DAIMON_REPORT__MCP_URL", "http://testserver/mcp")
    monkeypatch.setenv("DAIMON_REPORT__PUBLIC_URL_BASE", "http://reports.example.com")
    settings = load_settings(_env_file=None)
    fields: dict[str, object] = {"data_dir": tmp_path / "data"}
    fields.update(overrides)
    return settings.model_copy(update=fields)


async def _client(app: FakeASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_health_returns_200_with_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    app = main_mod.create_app(_settings(tmp_path=tmp_path, monkeypatch=monkeypatch))
    async with fake_seam_lifespan(app), await _client(app) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_lifespan_runs_resume_pass_exactly_once_and_starts_stops_the_sweep_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_seam_lifespan: FakeSeamLifespan,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resume_calls: list[dict[str, object]] = []

    async def fake_resume(**kwargs: object) -> int:
        resume_calls.append(kwargs)
        return 0

    monkeypatch.setattr(main_mod, "resume_running_threads", fake_resume)
    app = main_mod.create_app(_settings(tmp_path=tmp_path, monkeypatch=monkeypatch))

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        async with fake_seam_lifespan(app), await _client(app) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200

    assert len(resume_calls) == 1, "the resume pass must run exactly once, at startup"
    destroyed = [r for r in caplog.records if "destroyed" in r.getMessage().lower()]
    assert destroyed == [], "the sweep task must be cancelled and awaited, not abandoned"


async def test_exposes_reader_admin_and_upload_routes_at_their_documented_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = main_mod.create_app(_settings(tmp_path=tmp_path, monkeypatch=monkeypatch))
    paths = {getattr(route, "path", None) for route in app.routes}
    for expected in (
        "/health",
        "/r/{slug}",
        "/api/{slug}/state",
        "/api/{slug}/threads/{thread_id}",
        "/api/{slug}/ask",
        "/api/{slug}/threads/{thread_id}/cancel",
        "/api/{slug}/threads/{thread_id}/close",
        "/files/{slug}/{name}",
        "/admin/reports/{slug}",
        "/admin/reports/{slug}/recipients/{token}",
        "/publish/{capability_token}",
        "/upload/{turn_token}",
    ):
        assert expected in paths, f"missing documented route: {expected}"


async def test_admin_route_without_bearer_is_401_through_the_assembled_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    app = main_mod.create_app(_settings(tmp_path=tmp_path, monkeypatch=monkeypatch))
    async with fake_seam_lifespan(app), await _client(app) as client:
        resp = await client.put(
            "/admin/reports/some-report",
            json={
                "title": "t",
                "tenant_id": "tenant-1",
                "agent_name": "analyst",
                "cap_usd": "10",
                "seam_token": "seam-token-1",
                "recipients": [],
            },
        )
    assert resp.status_code == 401, "the admin bearer dependency must survive the app wiring"


async def test_schema_is_applied_on_construction_and_unknown_token_is_403_not_a_db_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    app = main_mod.create_app(_settings(tmp_path=tmp_path, monkeypatch=monkeypatch))
    async with fake_seam_lifespan(app), await _client(app) as client:
        resp = await client.get("/r/no-such-report", params={"k": "bogus"})
    assert resp.status_code == 403, "an empty, freshly-applied schema must not surface as a 500"

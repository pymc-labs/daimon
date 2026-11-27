"""Every response from the notebook host, proxied or not, says no-referrer."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.testclient import TestClient
from notebook_host.headers import ReferrerPolicyMiddleware

set_unjailed_test_env: Callable[[pytest.MonkeyPatch], None] = runpy.run_path(
    str(Path(__file__).parent / "conftest.py")
)["set_unjailed_test_env"]


def test_the_real_app_sends_no_referrer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The middleware is installed on the app the host actually serves."""
    import notebook_host.main as main_mod
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", "test-secret")
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)
    monkeypatch.setattr(main_mod, "can_apply_jail", lambda: False)

    with TestClient(main_mod.create_app(settings)) as client:
        ok = client.get("/health")
        missing = client.get("/n/nothing/")

    assert ok.headers["referrer-policy"] == "no-referrer", "a normal response carries the policy"
    assert missing.headers["referrer-policy"] == "no-referrer", "so does an error"


def test_the_policy_replaces_whatever_the_backend_sent() -> None:
    """A proxied marimo page cannot loosen the policy with its own header."""
    app = FastAPI()
    app.add_middleware(ReferrerPolicyMiddleware)

    @app.get("/page")
    def page() -> PlainTextResponse:  # pyright: ignore[reportUnusedFunction]
        return PlainTextResponse("hi", headers={"Referrer-Policy": "unsafe-url"})

    @app.get("/stream")
    def stream() -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        return StreamingResponse(iter([b"a", b"b"]))

    @app.get("/boom")
    def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise HTTPException(404, "gone")

    client = TestClient(app)

    for path in ("/page", "/stream", "/boom"):
        response = client.get(path)
        assert response.headers.get_list("referrer-policy") == ["no-referrer"], (
            f"{path} must carry exactly one no-referrer policy"
        )
    assert client.get("/stream").content == b"ab", "a streamed body passes through untouched"

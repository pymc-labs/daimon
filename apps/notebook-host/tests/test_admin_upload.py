"""Tests for the public PUT /upload/{token} capability-upload route.

Reuses the stubbed-spawner app builder pattern from test_admin.py: no real
marimo subprocess, wait_for_port monkeypatched True. Tokens are minted inline
with the same wire format the host verifies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import runpy
import subprocess
import unittest.mock
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from notebook_host.admin import AdminState, create_admin_router
from notebook_host.jail import SlugPaths, get_slug_paths

# `--import-mode=importlib` (root pyproject.toml) doesn't add this directory
# to sys.path, and there's no `__init__.py` here (one would collide with the
# top-level `tests` package name already claimed by `packages/core/tests`).
# `runpy` loads `conftest.py` by file path without touching sys.path or
# sys.modules, so a full monorepo `pytest` collection can't collide with
# similar sys.path tricks in other adapters' tests (see
# `packages/adapters/mcp/tests/tools/conftest.py`).
set_unjailed_test_env: Callable[[pytest.MonkeyPatch], None] = runpy.run_path(
    str(Path(__file__).parent / "conftest.py")
)["set_unjailed_test_env"]

_SECRET = "test-secret"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _mint(
    op: str,
    slug: str,
    *,
    max_bytes: int = 1_000_000,
    name: str | None = None,
    jti: str = "j1",
    ttl: int = 300,
    secret: str = _SECRET,
) -> str:
    payload = {
        "slug": slug,
        "op": op,
        "name": name,
        "max_bytes": max_bytes,
        "exp": int(datetime.now(UTC).timestamp()) + ttl,
        "jti": jti,
    }
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64(sig)}"


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    import notebook_host.admin as admin_mod
    from notebook_host.admin import AdminState, create_admin_router
    from notebook_host.config import load_settings

    monkeypatch.setenv("DAIMON_NOTEBOOK__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAIMON_NOTEBOOK__ADMIN_SECRET", _SECRET)
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_START", "8500")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MARIMO_PORT_END", "8501")
    monkeypatch.setenv("DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS", "2.0")
    set_unjailed_test_env(monkeypatch)
    settings = load_settings(_env_file=None)

    def spawner(
        slug: str,
        paths: SlugPaths,
        port: int,
        *,
        mode: str = "edit",
        jail_uid: int | None = None,
    ) -> subprocess.Popen[bytes]:
        m: unittest.mock.MagicMock = unittest.mock.MagicMock(spec=subprocess.Popen)
        m.poll.return_value = None
        m.pid = 4242
        return m  # type: ignore[return-value]

    async def _fake_wait(port: int, slug: str, timeout_s: float) -> bool:
        return True

    monkeypatch.setattr(admin_mod, "wait_for_port", _fake_wait)
    state = AdminState(
        settings=settings, processes={}, spawner=unittest.mock.MagicMock(side_effect=spawner)
    )
    app = FastAPI()
    app.include_router(create_admin_router(state))
    return TestClient(app, raise_server_exceptions=True), state


def test_upload_blog_writes_source_byte_exact_and_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _make_app(tmp_path, monkeypatch)
    source = b"# notebook\n" + b"x = 1\n" * 9000  # ~55 KB — the case inline source truncated on
    r = client.put(f"/upload/{_mint('blog', 'my-blog')}", content=source)
    assert r.status_code == 200, f"blog upload should succeed, got {r.status_code}: {r.text}"
    on_disk = get_slug_paths(tmp_path, "my-blog").notebook.read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(source).hexdigest(), (
        "source written byte-exact"
    )
    assert r.json()["slug"] == "my-blog", "response echoes the token-governed slug"


def test_upload_data_writes_raw_bytes_byte_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _make_app(tmp_path, monkeypatch)
    blob = b"\xde\xad\xbe\xef" * 250_000  # 1 MB — impossible to inline as base64
    tok = _mint("data", "my-blog", name="posterior.nc", max_bytes=1_500_000)
    r = client.put(f"/upload/{tok}", content=blob)
    assert r.status_code == 200, f"data upload should succeed, got {r.status_code}: {r.text}"
    assert (get_slug_paths(tmp_path, "my-blog").data / "posterior.nc").read_bytes() == blob, (
        "1 MB written byte-exact"
    )
    assert r.json()["path"] == "data/posterior.nc", "agent-visible read path returned"


def test_upload_notebook_returns_expires_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _make_app(tmp_path, monkeypatch)
    r = client.put(f"/upload/{_mint('notebook', 'scratch')}", content=b"# nb\n")
    assert r.status_code == 200, f"notebook upload should succeed, got {r.status_code}: {r.text}"
    assert "expires_at" in r.json(), "ephemeral notebook reports its TTL expiry"


def test_upload_rejects_forged_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_app(tmp_path, monkeypatch)
    r = client.put(f"/upload/{_mint('blog', 'x', secret='attacker')}", content=b"evil")
    assert r.status_code == 403, "a token signed by an unknown key is rejected"
    assert not get_slug_paths(tmp_path, "x").notebook.exists(), "no file written on a forged token"


def test_upload_single_use_replay_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_app(tmp_path, monkeypatch)
    tok = _mint("blog", "once", jti="reused")
    r1 = client.put(f"/upload/{tok}", content=b"# first\n")
    r2 = client.put(f"/upload/{tok}", content=b"# replay\n")
    assert r1.status_code == 200 and r2.status_code == 409, (
        "first use ok, replay of the same jti → 409"
    )
    assert get_slug_paths(tmp_path, "once").notebook.read_bytes() == b"# first\n", (
        "replay does not overwrite"
    )


def test_upload_oversize_body_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_app(tmp_path, monkeypatch)
    tok = _mint("blog", "big", max_bytes=1000)
    r = client.put(f"/upload/{tok}", content=b"z" * 5000)
    assert r.status_code == 413, "body over the token's max_bytes → 413"
    assert not get_slug_paths(tmp_path, "big").notebook.exists(), "no file written when oversize"

    retry = client.put(f"/upload/{tok}", content=b"z" * 500)
    assert retry.status_code == 409, (
        "the token was burned before the size check ran, so a retry after a rejected "
        "oversize upload is not reusable — that is the documented consequence of closing "
        "the concurrent-replay window before the body read"
    )


def test_upload_replay_rejected_across_a_fresh_admin_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capability token burned before a host restart must still be refused after one.

    Regression test for the defect: the old `AdminState.consumed` in-memory
    set only ever passed the single-use check because the process stayed
    alive across the two requests in a test. Building a *fresh* AdminState
    and app over the same data_dir — the shape of a real host restart —
    proves the burn now survives on disk rather than in that set.
    """
    client, state = _make_app(tmp_path, monkeypatch)
    tok = _mint("blog", "restart-test", jti="reused-across-restart")
    r1 = client.put(f"/upload/{tok}", content=b"# first\n")
    assert r1.status_code == 200, r1.text

    fresh_state = AdminState(settings=state.settings, processes={}, spawner=state.spawner)
    fresh_app = FastAPI()
    fresh_app.include_router(create_admin_router(fresh_state))
    fresh_client = TestClient(fresh_app, raise_server_exceptions=True)

    r2 = fresh_client.put(f"/upload/{tok}", content=b"# replay\n")
    assert r2.status_code == 409, "a token burned before a restart must still be refused after one"


def test_upload_data_with_no_name_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_app(tmp_path, monkeypatch)
    # a data token minted without a name → clean 400, not a cryptic empty-name error
    r = client.put(f"/upload/{_mint('data', 'my-blog', name=None)}", content=b"bytes")
    assert r.status_code == 400, f"data token without a name → 400, got {r.status_code}: {r.text}"


def test_upload_rejects_body_over_host_ceiling_under_token_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _make_app(tmp_path, monkeypatch)
    # token permits 2 MB, but the body (1.1 MB) exceeds the host source ceiling
    # (max_source_bytes = 1 MiB) — proves the two-tier cap, not just the token cap.
    tok = _mint("blog", "ceil", max_bytes=2_000_000)
    r = client.put(f"/upload/{tok}", content=b"z" * 1_100_000)
    assert r.status_code == 413, f"body over the host ceiling → 413, got {r.status_code}"
    assert not get_slug_paths(tmp_path, "ceil").notebook.exists(), (
        "no file written when over the host ceiling"
    )


def test_upload_returns_507_when_free_disk_below_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host without disk headroom must refuse uploads clearly, before burning.

    A full data volume used to surface as a 500 from deep inside the
    consumed-store write. The guard runs before the jti burn, so the same
    token stays usable once space is freed.
    """
    monkeypatch.setenv("DAIMON_NOTEBOOK__MIN_FREE_DISK_BYTES", str(2**60))
    client, _ = _make_app(tmp_path, monkeypatch)
    tok = _mint("blog", "no-room")
    r = client.put(f"/upload/{tok}", content=b"# nb\n")
    assert r.status_code == 507, f"no disk headroom → 507, got {r.status_code}: {r.text}"
    assert not get_slug_paths(tmp_path, "no-room").notebook.exists(), (
        "no file written without disk headroom"
    )

    monkeypatch.setenv("DAIMON_NOTEBOOK__MIN_FREE_DISK_BYTES", "0")
    retry_client, _ = _make_app(tmp_path, monkeypatch)
    retry = retry_client.put(f"/upload/{tok}", content=b"# nb\n")
    assert retry.status_code == 200, (
        "a token refused for missing headroom must not be burned — the same token "
        f"must succeed once space is freed, got {retry.status_code}: {retry.text}"
    )


def test_admin_put_notebook_returns_507_when_free_disk_below_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_NOTEBOOK__MIN_FREE_DISK_BYTES", str(2**60))
    client, _ = _make_app(tmp_path, monkeypatch)
    r = client.put(
        "/admin/notebooks/no-room",
        json={"source": "# nb\n"},
        headers={"Authorization": f"Bearer {_SECRET}"},
    )
    assert r.status_code == 507, f"no disk headroom → 507, got {r.status_code}: {r.text}"

"""Contract test fixtures for live MA API assertions.

All tests in this package are gated by @pytest.mark.contract and skip when
DAIMON_TEST_ANTHROPIC_API_KEY is not set. Module-scoped cleanup runs
delete_entire_workspace_for_testing before and after each test module.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import Settings
from daimon.core.ma import delete_entire_workspace_for_testing
from daimon.core.skill_zip import build_skill_zip

# Each test module in this package must set:
#   pytestmark = pytest.mark.contract
# at module level. pytestmark in conftest.py is NOT inherited by child test files.


def _require_api_key() -> str:
    """Read DAIMON_TEST_ANTHROPIC_API_KEY from env, skip if missing."""
    key = os.environ.get("DAIMON_TEST_ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("DAIMON_TEST_ANTHROPIC_API_KEY not set — contract tests skipped")
    return key


@pytest_asyncio.fixture(scope="module")
async def anthropic_client() -> AsyncAnthropic:
    """Real AsyncAnthropic client from env var. Skips if key is missing."""
    key = _require_api_key()
    return AsyncAnthropic(api_key=key)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _cleanup(anthropic_client: AsyncAnthropic) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Module-scoped cleanup: delete all workspace resources before and after."""
    await delete_entire_workspace_for_testing(
        anthropic_client, i_understand_this_destroys_all_tenants=True
    )
    yield
    await delete_entire_workspace_for_testing(
        anthropic_client, i_understand_this_destroys_all_tenants=True
    )


@pytest_asyncio.fixture(scope="module")
async def skill_zip_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a minimal skill zip for contract tests."""
    d = tmp_path_factory.mktemp("skill") / "contract-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: contract-skill\ndescription: contract test\n---\n")
    pkg = build_skill_zip(d)
    return pkg.path


@dataclass(frozen=True)
class LocalDaimonMCP:
    """Handle to a locally-running daimon-mcp uvicorn process.

    The same `jwt_secret_string` MUST be passed (wrapped in `SecretStr`) to
    whatever `McpSettings` the test threads through `run_turn`, so the JWT
    minted by `ensure_mcp_vault` validates against the running server.
    """

    public_url: str
    jwt_secret_string: str


def _pick_free_port() -> int:
    """Bind to an ephemeral port, release, return the number.

    Small TOCTOU window vs. another process — acceptable in the test sandbox.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest_asyncio.fixture(scope="module")
async def local_daimon_mcp() -> AsyncIterator[LocalDaimonMCP]:
    """Boot a real daimon-mcp ASGI app under uvicorn on a free local port.

    Module-scoped: one server per test module so multiple tests can reuse it.
    Readiness gate: polls GET /healthz until 200 (10s max).
    Teardown: sets `server.should_exit = True` and awaits the serve task.
    """
    test_db_url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not test_db_url:
        pytest.skip("DAIMON_DATABASE__TEST_URL not set — local_daimon_mcp fixture cannot boot")
    api_key = _require_api_key()

    port = _pick_free_port()
    public_url = f"http://127.0.0.1:{port}/mcp"
    jwt_secret_string = secrets.token_urlsafe(32)

    settings = Settings.model_validate(
        {
            "database": {"url": test_db_url},
            "anthropic": {"api_key": api_key},
            "mcp": {"jwt_secret": jwt_secret_string, "public_url": public_url},
            "discord": {"bot_token": "placeholder-never-invoked"},
        }
    )
    app = create_mcp_app(settings=settings)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    async with httpx.AsyncClient() as http:
        deadline = asyncio.get_event_loop().time() + 10.0
        ready = False
        while True:
            try:
                response = await http.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
                if response.status_code == 200:
                    ready = True
                    break
            except (httpx.ConnectError, httpx.ReadError):
                pass
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.1)

    if not ready:
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5.0)
        raise RuntimeError("daimon-mcp did not become ready on /healthz within 10s")

    try:
        yield LocalDaimonMCP(public_url=public_url, jwt_secret_string=jwt_secret_string)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except TimeoutError:
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await serve_task

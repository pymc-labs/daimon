"""The harness conformance kit against the current apps/harness, offline.

The harness runs as a child process on 127.0.0.1 (HARNESS_TEST_MODE=1, its real Agents SDK
loop) and its model endpoint is the scripted fake model server on 127.0.0.1. Nothing leaves the
machine and no provider credentials are set.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from mux.testing.fake_model_server import FakeModelServer
from mux.testing.fake_model_server.runner import serve
from mux.testing.harness_conformance import CASES, HarnessTarget, run_conformance

CODEX_BINARY_VARIABLE = "HARNESS_CODEX_BINARY"
LOOPS = ["agents_sdk", "codex"]
# How each loop names tools to the model. Codex exposes every harness tool as `harness_<name>`.
MODEL_NAMES: dict[str, dict[str, Callable[..., str]]] = {
    "agents_sdk": {},
    "codex": {
        "tool_name": lambda name: f"harness_{name}",
        "mcp_tool_name": lambda server, tool: f"harness_{server}__{tool}",
    },
}
PROCESS = Path(__file__).with_name("harness_process.py")
TOKEN = "conformance-token"  # not a secret: the harness listens on loopback only
SCRUBBED_PREFIXES = (
    "AZURE_",
    "OPENAI_",
    "ANTHROPIC_",
    "HARNESS_",
    "IDENTITY_",
    "MSI_",
    "GOOGLE_",
)
PROXY_VARIABLES = {"http_proxy", "https_proxy", "all_proxy"}


def offline_environment() -> dict[str, str]:
    """The parent's environment minus credentials and proxies, plus test mode."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(SCRUBBED_PREFIXES) and name.lower() not in PROXY_VARIABLES
    }
    return {**kept, "HARNESS_TEST_MODE": "1", "NO_PROXY": "*"}


class HarnessProcess:
    """One harness child process on a fixed home; ``restart`` kills it and starts another."""

    def __init__(self, home: Path, fake_model_url: str, log: Path, loop: str) -> None:
        self.home, self.fake_model_url, self.log, self.loop = home, fake_model_url, log, loop
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> str:
        with self.log.open("ab") as stderr:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(PROCESS),
                "--home",
                str(self.home),
                "--fake-model-url",
                self.fake_model_url,
                "--token",
                TOKEN,
                "--loop",
                self.loop,
                "--codex-binary",
                os.environ.get(CODEX_BINARY_VARIABLE, ""),
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                env=offline_environment(),
            )
        assert self.process.stdout is not None
        async with asyncio.timeout(60):
            line = (await self.process.stdout.readline()).decode().strip()
        assert line.startswith("PORT="), f"harness did not start; see {self.log}"
        return f"http://127.0.0.1:{line.removeprefix('PORT=')}"

    async def kill(self) -> None:
        """SIGKILL our own child: a crash, with no chance to clean up."""
        assert self.process is not None
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGKILL)
        await self.process.wait()

    async def restart(self) -> str:
        await self.kill()
        return await self.start()


@pytest.fixture(scope="module", params=LOOPS)
def loop(request: pytest.FixtureRequest) -> str:
    """Each model loop gets its own harness, fake model and full run of the cases."""
    name = str(request.param)
    if name == "codex" and not Path(os.environ.get(CODEX_BINARY_VARIABLE, "")).is_file():
        pytest.skip(f"{CODEX_BINARY_VARIABLE} does not point at the pinned Codex 0.162.1 binary")
    return name


@pytest.fixture(scope="module")
async def fake_model(loop: str) -> AsyncIterator[tuple[str, FakeModelServer]]:
    async with serve() as running:
        yield running


@pytest.fixture(scope="module")
async def target(
    loop: str,
    fake_model: tuple[str, FakeModelServer],
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[HarnessTarget]:
    url, fake = fake_model
    root = tmp_path_factory.mktemp("harness")
    harness = HarnessProcess(root / "home", url, root / "harness.log", loop)
    (root / "home").mkdir()
    base_url = await harness.start()
    try:
        yield HarnessTarget(
            base_url=base_url,
            token=TOKEN,
            model=fake,
            restart=harness.restart,
            timeout_s=30,
            **MODEL_NAMES[loop],
        )
    finally:
        await harness.kill()
    # Every model call the real loop made landed on the local fake, on Azure's Responses path.
    assert {request.api for request in fake.requests} <= {"responses"}
    assert all(request.headers["host"].startswith("127.0.0.1:") for request in fake.requests)
    assert all(request.path == "/openai/responses" for request in fake.requests)
    secrets = {name for name in offline_environment() if name.startswith(SCRUBBED_PREFIXES)}
    assert secrets == {"HARNESS_TEST_MODE"}


@pytest.mark.parametrize("case", [case.name for case in CASES])
async def test_case(case: str, target: HarnessTarget) -> None:
    [result] = await run_conformance(target, only=[case])
    print(f"{result.status.upper()} {result.name} {result.detail}")
    if result.status == "skip":
        pytest.skip(result.detail)
    assert result.status == "pass", result.detail

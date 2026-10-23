"""Scripted Managed Agents transport boundary for offline journeys."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from daimon.testing.ma import MARouter


class LockedRouter(MARouter):
    """A shared MA router whose unplanned requests fail inside MockTransport."""

    def __init__(self) -> None:
        super().__init__()
        self.unexpected_requests: list[str] = []

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        try:
            return super().dispatch(request)
        except AssertionError:
            self.unexpected_requests.append(f"{request.method} {request.url}")
            raise


@contextmanager
def block_real_http_transport() -> Iterator[None]:
    """Keep scripted SDK calls on MockTransport, even if a new client is added."""
    refusal = AssertionError("offline journey attempted real HTTP transport")
    with (
        patch.object(httpx.HTTPTransport, "handle_request", side_effect=refusal),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", side_effect=refusal),
    ):
        yield


def _offline_environment(artifact_root: Path) -> dict[str, str]:
    """Give the test process only local runtime settings, no provider credentials."""
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV", "PYTHONPATH")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["DAIMON_DATABASE__TEST_URL"] = os.environ["DAIMON_DATABASE__TEST_URL"]
    env["DAIMON_QA_ARTIFACT_ROOT"] = str(artifact_root)
    return env


def main() -> int:
    """Run the scripted journey and persist its evidence outside the checkout."""
    parser = argparse.ArgumentParser(description="Run offline QA journeys with a local test DB")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path.home() / ".local/state/daimon/qa-journeys",
    )
    args = parser.parse_args()
    if not os.environ.get("DAIMON_DATABASE__TEST_URL"):
        parser.error("DAIMON_DATABASE__TEST_URL must name a migrated local test database")
    root = args.artifact_root.resolve()
    # The test's writer validates privacy and repository placement as well.
    env = _offline_environment(root)
    test_file = Path(__file__).resolve().parent / "tests/test_offline.py"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", str(test_file)],
        env=env,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())

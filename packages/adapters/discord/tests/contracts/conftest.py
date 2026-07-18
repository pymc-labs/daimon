"""Contract test fixtures for the discord adapter's panel write paths.

Gated by `DAIMON_TEST_ANTHROPIC_API_KEY` (skip when unset). Each test in this
package MUST declare `pytestmark = pytest.mark.contract` at module level —
pytestmark in conftest.py is NOT inherited by child test files.

Cleanup discipline: this conftest deliberately does NOT register an autouse
`delete_entire_workspace_for_testing` fixture (cf. packages/core/tests/contracts/conftest.py).
The core variant nukes the entire MA workspace, which is hostile to running
against a live workspace with seeded agents (daimon, daimon-personal, real
user agents). Each test in this package is responsible for archiving the
resources IT created — use a per-test `RUN_TAG = uuid.uuid4().hex[:8]` and a
`finally:`-clause `_cleanup(client, *names)` helper (see
test_panel_roundtrip.py for the canonical pattern).
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic


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

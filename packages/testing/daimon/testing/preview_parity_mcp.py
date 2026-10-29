"""Old uvicorn entry point for `mcp_fixture_server`, with its old variable names.

Kept only so an existing deployment keeps starting; new deployments use
`daimon.testing.mcp_fixture_server:create_app`. Delete once nothing starts this.
"""

from __future__ import annotations

import os
from pathlib import Path

from daimon.testing.mcp_fixture_server import build_fixture_server
from daimon.testing.qa_mcp_gate import McpGate


def create_app() -> McpGate:
    """Uvicorn factory. Fail closed unless explicitly configured for preview."""
    if os.environ.get("DAIMON_PARITY_LANE") != "preview":
        raise ValueError("the fixture may only run in the preview lane")
    return build_fixture_server(
        token=os.environ["DAIMON_PREVIEW_PARITY_MCP_TOKEN"],
        journal_path=Path(os.environ.get("DAIMON_PARITY_CALL_LOG", "/tmp/parity-calls.jsonl")),
    )

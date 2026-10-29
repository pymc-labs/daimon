"""Synthetic MCP tools for tests and disposable test deployments, behind the QA host's gate.

The JSONL journal is the fixture's only side effect. One worker owns it; never
point it at real data. Caller identifies the authenticated fixture principal,
not a verified Daimon user (all agent attachments share one credential).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, cast

from daimon.testing.qa_mcp_gate import McpGate, RunBearer
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

TOOLS = (
    "lead_fixture_read",
    "lead_fixture_submit",
    "fixture_counter_read",
    "fixture_mislabeled_write",
)
#: Executed calls of these count as writes in `/calls`.
WRITES = ("lead_fixture_submit", "fixture_mislabeled_write")
CALLER = "mcp-fixture"


class CallJournal:
    """Append received and executed events; preserve evidence across restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)

    def append(self, phase: str, tool: object, args: object) -> None:
        event = {
            "at": dt.datetime.now(dt.UTC).isoformat(),
            "phase": phase,
            "tool": tool,
            "args": args,
            "caller": CALLER,
        }
        with self.path.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def observe(self, body: bytes) -> None:
        try:
            message: object = json.loads(body)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        message = cast(dict[str, Any], message)
        if message.get("method") != "tools/call":
            return
        params: object = message.get("params")
        if not isinstance(params, dict):
            self.append("received", None, params)
            return
        params = cast(dict[str, Any], params)
        self.append("received", params.get("name"), params.get("arguments", {}))

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def submitted_markers(self) -> list[str]:
        """Markers of executed submissions, oldest first. Reads never add to this."""
        markers: list[str] = []
        for event in self.events():
            args: object = event["args"]
            if (
                event["phase"] == "executed"
                and event["tool"] == "lead_fixture_submit"
                and isinstance(args, dict)
            ):
                markers.append(str(cast(dict[str, Any], args).get("marker")))
        return markers

    def readback(self) -> dict[str, Any]:
        events = self.events()
        return {
            "calls": events,
            "write_count": sum(
                event["phase"] == "executed" and event["tool"] in WRITES for event in events
            ),
        }


def build_fixture_server(*, token: str, journal_path: Path) -> McpGate:
    """Build a real Streamable HTTP MCP app with no database or provider access."""
    if len(token) < 32 or token != token.strip():
        raise ValueError("fixture bearer must be at least 32 characters without whitespace")
    journal = CallJournal(journal_path)
    mcp = FastMCP("mcp-fixture")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def lead_fixture_read(marker: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Read a synthetic lead marker; no lead database is connected."""
        journal.append("executed", "lead_fixture_read", {"marker": marker})
        return {"marker": marker, "fixture": "synthetic-lead"}

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
        )
    )
    def lead_fixture_submit(marker: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Record one synthetic submission; never contact a real lead system."""
        journal.append("executed", "lead_fixture_submit", {"marker": marker})
        return {"marker": marker, "status": "fixture-recorded"}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def fixture_counter_read() -> dict[str, list[str]]:  # pyright: ignore[reportUnusedFunction]
        """List the markers of every recorded submission, oldest first. A watch source."""
        journal.append("executed", "fixture_counter_read", {})
        return {"markers": journal.submitted_markers()}

    # Says it only reads, and writes: proves the watch list, not the hint, allows a poll.
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def fixture_mislabeled_write(marker: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Record one synthetic write while claiming to only read."""
        journal.append("executed", "fixture_mislabeled_write", {"marker": marker})
        return {"marker": marker, "status": "fixture-recorded"}

    # The deployment owns rotation/revocation; no temporary run TTL in this serving mode.
    bearer = RunBearer(
        token=token,
        expires_at=dt.datetime.max.replace(tzinfo=dt.UTC),
        revoked_marker=journal_path.parent / "revoked",
    )
    return McpGate(
        mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
        bearer=bearer,
        daimon_token=token,
        allowed_tools=TOOLS,
        readback=journal.readback,
        observe=journal.observe,
    )


def create_app() -> McpGate:
    """Uvicorn factory. Fail closed unless the fixture is explicitly enabled."""
    if os.environ.get("DAIMON_MCP_FIXTURE_ENABLED") != "true":
        raise ValueError("the MCP fixture runs only with DAIMON_MCP_FIXTURE_ENABLED=true")
    return build_fixture_server(
        token=os.environ["DAIMON_MCP_FIXTURE_TOKEN"],
        journal_path=Path(os.environ.get("DAIMON_MCP_FIXTURE_CALL_LOG", "/tmp/mcp-fixture.jsonl")),
    )

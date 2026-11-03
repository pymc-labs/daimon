"""Synthetic MCP tools for tests and disposable test deployments, behind the QA host's gate.

The JSONL journal is the fixture's only side effect. One worker owns it; never
point it at real data. Caller identifies the authenticated fixture principal,
not a verified Daimon user (all agent attachments share one credential).
A per-owner test bearer, when configured, is journaled under its own principal
instead, so `/calls` shows whose key a watch poll used.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from daimon.testing.qa_mcp_gate import McpGate, RunBearer
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.types import ASGIApp, Receive, Scope, Send

TOOLS = (
    "lead_fixture_read",
    "lead_fixture_submit",
    "fixture_counter_read",
    "fixture_mislabeled_write",
)
#: Executed calls of these count as writes in `/calls`.
WRITES = ("lead_fixture_submit", "fixture_mislabeled_write")
CALLER = "mcp-fixture"
#: The principal of the request being served; an owner bearer sets its own.
_CALLER: ContextVar[str] = ContextVar("fixture_caller", default=CALLER)


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
            "caller": _CALLER.get(),
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


class OwnerBearers:
    """Accept each test owner's own bearer as well as the agent's, journaled as that owner.

    The owner bearer is swapped for the agent bearer before the gate, so the gate's
    checks and tool allowlist are unchanged; only the journaled principal differs.
    """

    def __init__(self, app: ASGIApp, *, agent_token: str, owners: Mapping[str, str]) -> None:
        self._app = app
        self._agent = agent_token
        self._owners = dict(owners)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        principal = None
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            authorization = headers.get("authorization", "")
            presented = authorization[7:] if authorization.lower().startswith("bearer ") else ""
            for name, token in self._owners.items():
                if hmac.compare_digest(presented.encode(), token.encode()):
                    principal = name
        if principal is None:
            await self._app(scope, receive, send)
            return
        headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"authorization"]
        headers.append((b"authorization", f"Bearer {self._agent}".encode()))
        reset = _CALLER.set(principal)
        try:
            await self._app({**scope, "headers": headers}, receive, send)
        finally:
            _CALLER.reset(reset)


def _check_bearer(token: str) -> None:
    if len(token) < 32 or token != token.strip():
        raise ValueError("fixture bearer must be at least 32 characters without whitespace")


def build_fixture_server(
    *, token: str, journal_path: Path, owner_tokens: Mapping[str, str] | None = None
) -> McpGate | OwnerBearers:
    """Build a real Streamable HTTP MCP app with no database or provider access.

    `owner_tokens` maps a test owner principal to that owner's own bearer.
    """
    _check_bearer(token)
    for principal, owner_token in (owner_tokens or {}).items():
        _check_bearer(owner_token)
        if principal == CALLER or owner_token == token:
            raise ValueError("an owner bearer must differ from the agent's")
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
    gate = McpGate(
        mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
        bearer=bearer,
        daimon_token=token,
        allowed_tools=TOOLS,
        readback=journal.readback,
        observe=journal.observe,
    )
    if not owner_tokens:
        return gate
    return OwnerBearers(gate, agent_token=token, owners=owner_tokens)


def create_app() -> McpGate | OwnerBearers:
    """Uvicorn factory. Fail closed unless the fixture is explicitly enabled."""
    if os.environ.get("DAIMON_MCP_FIXTURE_ENABLED") != "true":
        raise ValueError("the MCP fixture runs only with DAIMON_MCP_FIXTURE_ENABLED=true")
    return build_fixture_server(
        token=os.environ["DAIMON_MCP_FIXTURE_TOKEN"],
        journal_path=Path(os.environ.get("DAIMON_MCP_FIXTURE_CALL_LOG", "/tmp/mcp-fixture.jsonl")),
        owner_tokens=json.loads(os.environ.get("DAIMON_MCP_FIXTURE_OWNER_TOKENS") or "{}"),
    )

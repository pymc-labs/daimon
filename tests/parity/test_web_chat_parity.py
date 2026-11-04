"""Nothing may strictly require the web app: every web write has a chat path.

Walks the real web app's write routes (POST, PUT, PATCH, DELETE) and requires each
`(method, path)` to be mapped in `web_chat_parity.json` to one of:

- `{"tool": name}`: an MCP tool; it serves every chat platform it is tagged for. It may also
  name the chat panels that do the same (`panel`), as a record.
- `{"panel": {platform: module}}`: a chat panel per platform, all three.
- `{"exempt": reason}`: configures the web app itself (web chat, web sign-in, page serving).
- `{"gap": note}`: no chat path yet. A gap may also carry a partial `panel` or `tool`.

`not_on` names a platform that deliberately has no such feature. Tool names are checked
against the live MCP registry and panel modules must import. The gap counts only go down:
fill a gap, then lower `MAX_GAPS` (and `MAX_READ_GAPS` for the read-only pages in
`read_gaps`) in the same change.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.web.app import create_app
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import Settings, WebSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.mcp_identity import encode_seed, generate_signing_key
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parents[2]
TABLE = Path(__file__).with_name("web_chat_parity.json")
# Only these may go down: fill a gap, then lower the literal.
MAX_GAPS = 21
MAX_READ_GAPS = 12

PLATFORMS = frozenset({"discord", "slack", "teams"})
WRITE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
KINDS = frozenset({"tool", "panel", "exempt", "gap"})
ROW_KEYS = KINDS | {"not_on"}

SPEC = importlib.util.spec_from_file_location(
    "mcp_tool_catalogue", ROOT / "scripts/generate_mcp_tool_catalogue.py"
)
assert SPEC is not None and SPEC.loader is not None
catalogue = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = catalogue  # dataclasses resolve their module here
SPEC.loader.exec_module(catalogue)


def _app_routes() -> list[APIRoute]:
    node = {"identity_server": "data", "client_id": "c", "client_secret": "s"}
    key = generate_signing_key()
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        mcp={"public_url": "https://daimon.example.com/mcp"},  # pyright: ignore[reportArgumentType]
        mcp_identity={  # pyright: ignore[reportArgumentType]
            "signing_key": encode_seed(key),
            "servers": [
                {
                    "name": "data",
                    "url": "https://data.example.com/mcp",
                    "issuer": "6f1b1d4e-0f3c-4a52-9a53-0d3a4a8e1f10",
                    "audience": "https://data.example.com",
                }
            ],
        },
        web={"public_url": "https://web.example.com", "nodes": {"data_server": node}},  # pyright: ignore[reportArgumentType]
    )
    runtime = WebRuntime(
        settings=settings,
        web=WebSettings(public_url="https://web.example.com"),  # pyright: ignore[reportArgumentType]
        sessionmaker=async_sessionmaker(class_=AsyncSession),
        http_client=httpx.AsyncClient(),
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
        identity_key=key,
    )
    return [r for r in create_app(runtime).routes if isinstance(r, APIRoute)]


def _routes(methods: frozenset[str]) -> set[str]:
    return {f"{m} {r.path}" for r in _app_routes() for m in r.methods if m in methods}


def _load() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(TABLE.read_text()))


def _tools() -> dict[str, set[str]]:
    """Every tool of the /mcp registry mapped to the chat platforms it serves."""
    with catalogue._isolated_env():  # pyright: ignore[reportPrivateUsage]
        main, _hub = asyncio.run(catalogue._collect_surfaces())  # pyright: ignore[reportPrivateUsage]
    return {t.name: (set(t.tags) & PLATFORMS) or set(PLATFORMS) for t in main}


def test_every_write_route_is_mapped() -> None:
    routes, table = _routes(WRITE), set(_load()["routes"])
    assert not routes - table, (
        "Web write routes with no row in tests/parity/web_chat_parity.json. Give each a chat "
        "tool, a panel on every platform, an exemption, or (last resort) a gap:\n"
        + "\n".join(sorted(routes - table))
    )
    assert not table - routes, "Rows for routes that no longer exist:\n" + "\n".join(
        sorted(table - routes)
    )


def test_rows_are_well_formed() -> None:
    for route, row in _load()["routes"].items():
        assert set(row) <= ROW_KEYS and set(row) & KINDS, f"{route}: bad keys {sorted(row)}"
        assert all(isinstance(row[k], str) and row[k] for k in ("exempt", "gap") if k in row), route
        if "exempt" in row:
            assert set(row) == {"exempt"}, f"{route}: an exemption stands alone"
        if "tool" in row:
            assert set(row) <= {"tool", "not_on", "panel", "gap"}, route
        panel = row.get("panel", {})
        assert set(panel) <= PLATFORMS, f"{route}: unknown platform in {sorted(panel)}"
        if "panel" in row and "gap" not in row and "tool" not in row:
            assert set(panel) == PLATFORMS, f"{route}: a panel row covers all three platforms"
        if "gap" in row:
            assert "tool" not in row, f"{route}: a gap names no tool yet"
        if "panel" in row and "gap" in row:
            assert set(panel) != PLATFORMS, f"{route}: a full panel is not a gap"
        assert set(row.get("not_on", {})) <= PLATFORMS, route


def test_tools_exist_and_cover_every_platform() -> None:
    tools = _tools()
    for route, row in _load()["routes"].items():
        if "tool" not in row:
            continue
        assert row["tool"] in tools, f"{route}: no MCP tool named {row['tool']}"
        missing = PLATFORMS - tools[row["tool"]] - set(row.get("not_on", {}))
        assert not missing, f"{route}: tool {row['tool']} is not served on {sorted(missing)}"


def test_panel_modules_import() -> None:
    for route, row in _load()["routes"].items():
        for platform, module in row.get("panel", {}).items():
            assert module.startswith(f"daimon.adapters.{platform}."), f"{route}: {module}"
            importlib.import_module(module)


def test_gap_ratchet() -> None:
    gaps = sorted(r for r, row in _load()["routes"].items() if "gap" in row)
    assert len(gaps) <= MAX_GAPS, (
        f"Web/chat gaps grew: {len(gaps)} > {MAX_GAPS}. Add a chat tool or panel for the new "
        "write instead of a gap; never raise MAX_GAPS. After filling gaps, lower it.\n"
        + "\n".join(gaps)
    )


def test_read_gap_ratchet() -> None:
    reads = _load()["read_gaps"]
    known = _routes(frozenset({"GET"}))
    assert set(reads) <= known, f"Read gaps for GET routes that do not exist: {set(reads) - known}"
    assert all(isinstance(v, str) and v for v in reads.values())
    assert len(reads) <= MAX_READ_GAPS, (
        f"Read-only web pages with no chat equivalent grew: {len(reads)} > {MAX_READ_GAPS}. "
        "Never raise MAX_READ_GAPS; after filling gaps, lower it."
    )


@pytest.mark.parametrize("key", ["routes", "read_gaps"])
def test_table_is_sorted(key: str) -> None:
    keys = list(_load()[key])
    assert keys == sorted(keys), f"Keep {key} sorted so reviews diff cleanly."

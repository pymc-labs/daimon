"""Generate docs/mcp-tools.md from the live MCP tool registry.

Builds the real server the way ``create_mcp_app`` builds it in production —
fully-configured Settings so no registration group is silently skipped — and
lists what came back from ``mcp.local_provider.list_tools()``: name, defining
module, the first sentence of the tool's own description, and the visibility
tags that decide who can call it.

No database and no network. The engine is constructed from a placeholder DSN
and never connected, the Anthropic client is never called, and no tool is
invoked; this is registration and introspection only. The settings are built
with the process's ``DAIMON_*``/``STRIPE_*`` variables and any ``.env``
suppressed, so the page does not depend on the machine that generated it.

The same registry is snapshotted by
``packages/adapters/mcp/tests/test_tool_schema_snapshot.py``, which locks the
full schema (descriptions and parameters) rather than a summary. That
snapshot is a syrupy serialization, not a source this page can read: it
records neither the tags nor the defining module, which are what this page
groups and marks by. Both therefore build the registry the same way, and the
same fully-configured-settings reasoning applies to both — a partially
configured Settings would quietly produce a shorter catalogue.

Run: uv run python scripts/generate_mcp_tool_catalogue.py [--check]
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import inspect
import os
import re
import sys
import textwrap
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    GeminiSettings,
    McpSettings,
    NotebookSettings,
    Settings,
    SlackSettings,
)
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import DeploymentDefault
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.tools.tool import Tool
from pydantic import HttpUrl, PostgresDsn, SecretStr

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOGUE_PATH = REPO_ROOT / "docs" / "mcp-tools.md"

TOOLS_PACKAGE = "daimon.adapters.mcp.tools."
WRAP_WIDTH = 88

# Env vars that would otherwise reach the settings models through
# pydantic-settings' environment source and change which tools register
# (DAIMON_GEMINI__API_KEY gates one, DAIMON_DISCORD__/SLACK__ gate eleven).
# Cleared while the app is built so a contributor's shell cannot change the
# committed page.
ISOLATED_ENV_PREFIXES = ("DAIMON_", "STRIPE_")
ISOLATED_ENV_NAMES = ("MCP_PUBLIC_URL",)

# Registration groups `create_mcp_app` skips silently rather than failing
# when their settings are absent. If one of these modules contributes nothing
# the catalogue is quietly short, so it is an error instead.
CONDITIONAL_MODULES = ("channels", "media")

# What each visibility tag means, from the `Visibility(False, tags=...)`
# baselines in `server.py` and the re-enabling in
# `middleware/mcp_identity.py`. Every tagged tool is hidden by default and
# only restored for a caller the middleware matches.
TAG_LABELS: dict[str, str] = {
    "admin": "admin only",
    "agent-chat": "agent tokens only",
    "discord": "Discord callers",
    "slack": "Slack callers",
}
UNTAGGED_LABEL = "all callers"

# The hub mounts are a second surface: one FastMCP app per platform
# (hub/app.py:mount_hub_apps), each behind that platform's OAuth proxy and
# mounted only when its DAIMON_HUB__* client credentials are configured.
HUB_NOTE = (
    "A second surface, separate from the tools above: one app per platform, mounted at "
    "`/discord/mcp` and `/slack/mcp` behind that platform's OAuth login, and present "
    "only when the matching `DAIMON_HUB__*` client credentials are set. These tools "
    "carry no visibility tags, so a logged-in caller sees all of them; each takes a "
    "`daimon_id` from `list_daimons`, because one person reaches several workspaces "
    "here."
)

# Sentence-final periods that are not sentence ends.
ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.")

_SENTENCE_BREAK = re.compile(r"[.!?](?=\s)")


@dataclass(frozen=True)
class ToolRow:
    name: str
    purpose: str
    visibility: str


@dataclass(frozen=True)
class ToolGroup:
    """One module's worth of tools, as one `##` section."""

    module_name: str
    summary: str
    rows: tuple[ToolRow, ...]


@contextmanager
def _isolated_env() -> Iterator[None]:
    """Run with daimon's env vars removed, so the generated page depends on
    the code and nothing else."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(ISOLATED_ENV_PREFIXES) or key in ISOLATED_ENV_NAMES:
            del os.environ[key]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _fully_configured_settings() -> Settings:
    """Every optional settings group populated so the whole registry
    registers. A partially configured Settings omits tools silently, which
    is exactly the partial truth a generated catalogue must not ship."""
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("placeholder")),
        mcp=McpSettings(
            jwt_secret=SecretStr("a" * 32),
            public_url=HttpUrl("https://mcp.example.com/mcp"),
        ),
        discord=DiscordSettings(bot_token=SecretStr("placeholder")),
        slack=SlackSettings(
            signing_secret=SecretStr("placeholder"),
            app_token=SecretStr("xapp-placeholder"),
        ),
        crypto=CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),)),
        gemini=GeminiSettings(api_key=SecretStr("placeholder")),
        notebook=NotebookSettings(
            host_url=HttpUrl("http://notebook-host:8001"),
            admin_secret=SecretStr("placeholder"),
        ),
    )


async def _collect_surfaces() -> tuple[Sequence[Tool], Sequence[Tool]]:
    """Return (the /mcp tool registry, the hub-mount tool registry)."""
    settings = _fully_configured_settings()
    # Lazily built and never connected: SQLAlchemy opens no connection until
    # a session is used, and nothing here uses one.
    sessionmaker = build_session_factory(build_engine(str(settings.database.url)))
    app = create_mcp_app(
        settings=settings,
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=AsyncAnthropic(api_key="placeholder"),
        billing_config=None,
    )
    hub = build_hub_app(
        platform="discord",
        runtime=McpRuntime(
            session_factory=sessionmaker,
            client=AsyncAnthropic(api_key="placeholder"),
            settings=settings,
            deployment_default=DeploymentDefault(environment_name=None),
        ),
        auth=StaticTokenVerifier(tokens={}),
        billing_config=None,
    )
    return await app.state.mcp.local_provider.list_tools(), await hub.local_provider.list_tools()


def _first_sentence(description: str | None) -> str:
    """The tool's one-line purpose: the first sentence of its description.

    Several docstrings open with the question the tool answers ("What keys
    does an agent have?") and state the purpose in the sentence after, so a
    purpose ending in a question mark takes one more sentence with it.
    """
    if not description:
        return ""
    collapsed = " ".join(description.split())
    ends = [
        match.end()
        for match in _SENTENCE_BREAK.finditer(collapsed)
        if not collapsed[: match.end()].lower().endswith(ABBREVIATIONS)
    ]
    if not ends:
        return collapsed
    for end in ends:
        if not collapsed[:end].endswith("?"):
            return collapsed[:end]
    return collapsed[: ends[-1]]


def _visibility(tool: Tool) -> str:
    labels = sorted(TAG_LABELS[tag] for tag in tool.tags if tag in TAG_LABELS)
    unknown = sorted(tag for tag in tool.tags if tag not in TAG_LABELS)
    if unknown:
        raise RuntimeError(
            f"{tool.name} carries unknown tag(s) {unknown}; add them to TAG_LABELS "
            "once server.py says what they gate"
        )
    return ", ".join(labels) if labels else UNTAGGED_LABEL


def _module_name(tool: Tool) -> str:
    module = inspect.getmodule(tool.fn)  # pyright: ignore[reportUnknownMemberType]
    if module is None:
        raise RuntimeError(f"cannot locate the module that defines {tool.name}")
    return module.__name__


def _module_summary(module_name: str) -> str:
    module = sys.modules[module_name]
    doc = inspect.getdoc(module)
    return _first_sentence(doc.split("\n\n")[0]) if doc else ""


def _group_tools(tools: Sequence[Tool]) -> list[ToolGroup]:
    by_module: dict[str, list[Tool]] = {}
    for tool in tools:
        by_module.setdefault(_module_name(tool), []).append(tool)
    return [
        ToolGroup(
            module_name=module_name,
            summary=_module_summary(module_name),
            rows=tuple(
                ToolRow(
                    name=tool.name,
                    purpose=_first_sentence(tool.description),
                    visibility=_visibility(tool),
                )
                for tool in sorted(by_module[module_name], key=lambda t: t.name)
            ),
        )
        for module_name in sorted(by_module)
    ]


def _wrap(text: str) -> list[str]:
    return textwrap.wrap(" ".join(text.split()), width=WRAP_WIDTH) or [""]


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _render_group(group: ToolGroup, *, heading: str, note: str = "") -> list[str]:
    lines = [f"## {heading}", ""]
    if group.summary:
        lines.extend([*_wrap(group.summary), ""])
    if note:
        lines.extend([*_wrap(note), ""])
    lines.extend(["| Tool | Who can call it | Purpose |", "| --- | --- | --- |"])
    lines.extend(
        f"| `{row.name}` | {row.visibility} | {_cell(row.purpose)} |" for row in group.rows
    )
    lines.append("")
    return lines


def _header(total: int, hub_total: int) -> list[str]:
    tag_lines = [
        f"- **{label}** — carries the `{tag}` tag." for tag, label in sorted(TAG_LABELS.items())
    ]
    return [
        "# MCP tool catalogue",
        "",
        *_wrap(
            f"The {total} tools daimon's MCP server registers, plus the {hub_total} on the "
            "hub login mounts. Generated from the live registry by "
            "`scripts/generate_mcp_tool_catalogue.py` — edit the tool's docstring, not "
            "this page. CI fails when the two disagree."
        ),
        "",
        *_wrap(
            "Each section is one module under "
            "`packages/adapters/mcp/daimon/adapters/mcp/tools/`. The purpose column is "
            "the first sentence of the docstring the model itself reads; the full text "
            "and the parameter schema live in the tool's source."
        ),
        "",
        "## Who can call what",
        "",
        *_wrap(
            "Every tool below is registered on one server, and an identity middleware "
            "decides per request which of them a caller may see. Untagged tools are "
            "visible to everyone; a tagged tool is hidden by default and restored only "
            "for a matching caller."
        ),
        "",
        *tag_lines,
        "",
        *_wrap(
            "A CLI token matches no platform tag, so it sees neither the Discord nor "
            "the Slack tools. An agent token is narrowed to the agent-chat tools alone "
            "— everything else is disabled for it, admin tools included."
        ),
        "",
        *_wrap(
            "A caller does not necessarily receive this list in one response: the "
            "server applies a BM25 search transform, so an ordinary session discovers "
            "tools by searching the catalogue rather than listing it in full. Sessions "
            "narrowed to agent-chat tools skip the transform and see their tools "
            "directly."
        ),
        "",
    ]


def render_catalogue() -> str:
    with _isolated_env():
        main_tools, hub_tools = asyncio.run(_collect_surfaces())

    groups = _group_tools(main_tools)
    missing = [
        name
        for name in CONDITIONAL_MODULES
        if not any(group.module_name == TOOLS_PACKAGE + name for group in groups)
    ]
    if missing:
        raise RuntimeError(
            f"no tools registered from {missing}; these groups are skipped silently "
            "when their settings are absent, so the catalogue would be short"
        )

    lines = _header(len(main_tools), len(hub_tools))
    for group in groups:
        lines.extend(
            _render_group(group, heading=f"`{group.module_name.removeprefix(TOOLS_PACKAGE)}`")
        )

    for group in _group_tools(hub_tools):
        lines.extend(
            _render_group(
                group,
                heading=f"Hub login mounts: `{group.module_name.removeprefix(TOOLS_PACKAGE)}`",
                note=HUB_NOTE,
            )
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate docs/mcp-tools.md from the live MCP tool registry.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed page matches generator output; exit nonzero on drift.",
    )
    args = parser.parse_args(argv)

    generated = render_catalogue()

    if args.check:
        current = CATALOGUE_PATH.read_text() if CATALOGUE_PATH.exists() else ""
        if current != generated:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                generated.splitlines(keepends=True),
                fromfile="docs/mcp-tools.md (committed)",
                tofile="docs/mcp-tools.md (generated)",
            )
            sys.stderr.writelines(diff)
            print(
                "\ndocs/mcp-tools.md is out of date. "
                "Run: uv run python scripts/generate_mcp_tool_catalogue.py",
                file=sys.stderr,
            )
            return 1
        return 0

    CATALOGUE_PATH.write_text(generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())

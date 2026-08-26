"""Locks the model-facing MCP tool surface: every tool name, description and
parameter schema the server renders to the model, so a change to any of them
shows up as a reviewable snapshot diff instead of shipping unreviewed.

To intentionally update the snapshot after a deliberate schema/docstring
change, run:
    uv run pytest packages/adapters/mcp/tests/test_tool_schema_snapshot.py --snapshot-update
then review the diff in ``__snapshots__/`` before committing.

Do NOT run ``--snapshot-update`` under ``-n auto`` (pytest-xdist) — xdist
workers only persist a subset of newly generated snapshots.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet
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
from daimon.testing.ma import build_stub_anthropic
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from syrupy.assertion import SnapshotAssertion

pytestmark = pytest.mark.asyncio

# Tool names whose absence would mean a whole registration group silently
# failed to register (e.g. a Settings regression that drops discord/slack or
# leaves a group unconfigured) — checked before the snapshot comparison so
# that failure mode is loud rather than a quietly smaller snapshot.
_MUST_CONTAIN = {
    "cancel_turn",
    "create_environment",
    "send_message",
    "sync_skills",
    "set_agent_default",
    "update_agent",
}


def _fully_configured_settings() -> Settings:
    """Every optional settings group populated so the whole tool registry
    registers — a partially-configured Settings would silently omit tools,
    which is the exact partial-truth failure this test exists to catch."""
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(
            jwt_secret=SecretStr("a" * 32),
            public_url=HttpUrl("https://x/mcp"),
        ),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
        slack=SlackSettings(
            signing_secret=SecretStr("test-signing-secret"),
            app_token=SecretStr("xapp-test-token"),
        ),
        crypto=CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),)),
        gemini=GeminiSettings(api_key=SecretStr("gemini-test-key")),
        notebook=NotebookSettings(
            host_url=HttpUrl("http://notebook-host:8001"),
            admin_secret=SecretStr("notebook-admin-secret"),
        ),
    )


def _strip_trailing_whitespace(description: str | None) -> str | None:
    """Strip trailing whitespace from each line of a tool description.

    Several tool docstrings carry incidental trailing spaces on otherwise
    blank lines between paragraphs — invisible to the model, but the repo's
    trailing-whitespace pre-commit hook rewrites the committed ``.ambr`` file
    to remove exactly that whitespace. Normalizing here keeps the snapshot
    comparison stable against a file the hook itself would otherwise mutate
    out from under it.
    """
    if description is None:
        return None
    return "\n".join(line.rstrip() for line in description.split("\n"))


async def test_rendered_tool_schema_matches_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession],
    snapshot: SnapshotAssertion,
) -> None:
    """The full rendered tool registry — name, description, parameters — is
    what the model actually sees. No tool is invoked; this is registration
    and introspection only, against an injected transport-level fake so no
    network or real key is required."""
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    mcp = app.state.mcp
    tools = await mcp.local_provider.list_tools()

    rendered: dict[str, dict[str, object]] = {
        tool.name: {
            "description": _strip_trailing_whitespace(tool.description),
            "parameters": tool.parameters,
        }
        for tool in tools
    }
    rendered = dict(sorted(rendered.items()))

    assert rendered, "rendered tool registry must not be empty"
    missing = _MUST_CONTAIN - rendered.keys()
    assert not missing, f"expected tool group(s) missing from the rendered registry: {missing}"

    assert rendered == snapshot, "rendered tool schema drifted from the committed snapshot"


def _non_null_branch(container: dict[str, Any]) -> dict[str, Any]:
    """Return the non-null branch of a nullable ``anyOf`` wrapper, or the
    container itself when it isn't one."""
    any_of = container.get("anyOf")
    if any_of is None:
        return container
    branch = next((b for b in any_of if b.get("type") != "null"), None)
    assert branch is not None, f"no non-null branch found in anyOf: {any_of}"
    return branch


def _resolve_ref(schema: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a ``#/$defs/<name>`` pointer against the tool's top-level
    JSON Schema, reading the def name from the ref string itself rather than
    hardcoding it — an SDK type rename should not produce a false failure."""
    prefix = "#/$defs/"
    assert ref.startswith(prefix), f"unexpected $ref shape: {ref!r}"
    def_name = ref.removeprefix(prefix)
    defs = schema.get("$defs", {})
    assert def_name in defs, f"$ref target {def_name!r} missing from $defs"
    return defs[def_name]  # type: ignore[no-any-return]


async def test_create_environment_exposes_nested_pip_package_list(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """create_environment takes one nested model, and the whole reason a
    prose docstring can be the complete fix for "the model never sets pip
    packages" is that this nested field reaches the model as a plain string
    array. If a dependency upgrade ever flattens it to an opaque object, this
    test goes red and the fix has to change shape — the snapshot alone would
    just be regenerated around the new (broken) shape."""
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    mcp = app.state.mcp
    tools = await mcp.local_provider.list_tools()
    tool = next((t for t in tools if t.name == "create_environment"), None)
    assert tool is not None, "create_environment must be registered"
    schema = tool.parameters
    assert schema is not None, "create_environment must expose a parameter schema"

    spec_prop = schema["properties"]["spec"]
    assert "$ref" in spec_prop, f"hop 1 (spec): expected a $ref, got {spec_prop}"
    spec_def = _resolve_ref(schema, spec_prop["$ref"])

    config_prop = spec_def["properties"]["config"]
    config_branch = _non_null_branch(config_prop)
    assert "$ref" in config_branch, f"hop 2 (spec.config): expected a $ref, got {config_branch}"
    cloud_config_def = _resolve_ref(schema, config_branch["$ref"])

    packages_prop = cloud_config_def["properties"]["packages"]
    packages_branch = _non_null_branch(packages_prop)
    assert "$ref" in packages_branch, (
        f"hop 3 (spec.config.packages): expected a $ref, got {packages_branch}"
    )
    packages_def = _resolve_ref(schema, packages_branch["$ref"])

    pip_prop = packages_def["properties"]["pip"]
    pip_branch = _non_null_branch(pip_prop)
    assert pip_branch.get("type") == "array", (
        f"hop 4 (spec.config.packages.pip): expected an array, got {pip_branch}"
    )
    items = pip_branch.get("items", {})
    assert items.get("type") == "string", (
        f"hop 5 (spec.config.packages.pip.items): expected type 'string', got {items}"
    )

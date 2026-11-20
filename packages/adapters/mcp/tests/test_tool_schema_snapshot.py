"""Locks the model-facing MCP tool surface: every tool name, description and
parameter schema the server renders to the model, so a change to any of them
shows up as a reviewable snapshot diff instead of shipping unreviewed.

Each tool is its own JSON file under ``__snapshots__/``, so two changes to
different tools never conflict. A removed tool leaves an unused snapshot, which
fails the run. To intentionally update the snapshots after a deliberate
schema/docstring change, run:
    uv run pytest packages/adapters/mcp/tests/test_tool_schema_snapshot.py --snapshot-update
then review the diff in ``__snapshots__/`` before committing.

Do NOT run ``--snapshot-update`` under ``-n auto`` (pytest-xdist) — xdist
workers only persist a subset of newly generated snapshots.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
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
    MicrosoftAccountsSettings,
    NotebookSettings,
    RepoMemorySettings,
    Settings,
    SlackSettings,
    TurnSettings,
)
from daimon.core.features.tool_gate import WEB_APP_TOOLS
from daimon.core.scope import DeploymentDefault
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.json import JSONSnapshotExtension

# Tool names whose absence would mean a whole registration group silently
# failed to register (e.g. a Settings regression that drops discord/slack or
# leaves a group unconfigured) — checked before the snapshot comparison so
# that failure mode is loud rather than a quietly smaller snapshot.
_MUST_CONTAIN = {
    "create_watch",
    "list_watches",
    "lead_intake_resolve",
    "set_routine_sandbox_tools",
    "create_environment",
    "send_message",
    "sync_skills",
    "set_agent_default",
    "update_agent",
    "repo_memory_save",
    "repo_memory_delete",
}


# Tools that say "web" without meaning the web app, and why.
_WEB_WORDS = {
    "set_agent_builtin_tool": "the agent's web search built-in",
    "web_fetch": "a public web page",
    "replace_channel_file": "Microsoft's Office for the web",
    "list_audit_log": "the stored `web` source value",
    "list_monitored_activity": "web chat, one of the places admins monitor",
    "get_monitored_conversation": "web chat, one of the places admins monitor",
}


@pytest.fixture
def schema_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Registration and introspection must not connect to a database."""
    return async_sessionmaker()


def _fully_configured_settings() -> Settings:
    """Every optional settings group populated so the whole tool registry
    registers — a partially-configured Settings would silently omit tools,
    which is the exact partial-truth failure this test exists to catch."""
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        repo_memory=RepoMemorySettings(enabled=True),
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        turn=TurnSettings(channel_backends=True, agent_backends=True),
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
        microsoft_accounts=MicrosoftAccountsSettings(
            client_id="client-id",
            client_secret=SecretStr("test-client-secret"),
            tenant_id="00000000-0000-0000-0000-000000000001",
        ),
        gemini=GeminiSettings(api_key=SecretStr("gemini-test-key")),
        notebook=NotebookSettings(
            host_url=HttpUrl("http://notebook-host:8001"),
            admin_secret=SecretStr("notebook-admin-secret"),
        ),
    )


def _snapshot_value(tool: Any) -> dict[str, object]:
    """One tool as its snapshot file holds it. The description is a list of
    lines so a wording change is a line diff, not one escaped string."""
    description: str | None = tool.description
    return {
        "description": None if description is None else description.split("\n"),
        "parameters": tool.parameters,
    }


def _assert_each_matches_snapshot(values: dict[str, object], snapshot: SnapshotAssertion) -> None:
    """Compare every value with its own snapshot file, named after the tool."""
    for name, value in values.items():
        assert value == snapshot(name=name, extension_class=JSONSnapshotExtension), (
            f"{name} drifted from its committed snapshot"
        )


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Registry introspection constructs an engine but never opens a connection."""
    engine = create_async_engine("postgresql+asyncpg://placeholder:placeholder@localhost/unused")
    try:
        yield async_sessionmaker(engine)
    finally:
        await engine.dispose()


async def test_rendered_tool_schema_matches_snapshot(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    snapshot: SnapshotAssertion,
) -> None:
    """The full rendered tool registry — name, description, parameters — is
    what the model actually sees. No tool is invoked; this is registration
    and introspection only, against an injected transport-level fake so no
    network or real key is required."""
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=schema_sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    mcp = app.state.mcp
    tools = await mcp.local_provider.list_tools()

    rendered: dict[str, object] = {tool.name: _snapshot_value(tool) for tool in tools}

    assert rendered, "rendered tool registry must not be empty"
    missing = _MUST_CONTAIN - rendered.keys()
    assert not missing, f"expected tool group(s) missing from the rendered registry: {missing}"

    create = next(tool for tool in tools if tool.name == "create_watch")
    assert create.parameters["properties"]["source"]["enum"] == ["poll", "callback"]
    assert "page_id" not in create.parameters["properties"]

    named = {n for n, t in rendered.items() if re.search(r"(?i)\bweb\b", str(t))}
    assert named - WEB_APP_TOOLS == _WEB_WORDS.keys(), (
        f"{sorted(named - WEB_APP_TOOLS - _WEB_WORDS.keys())} name the web: add them to "
        "WEB_APP_TOOLS (hidden while web_awareness is Off), word them without it, or list them "
        "in _WEB_WORDS with a reason"
    )
    _assert_each_matches_snapshot(rendered, snapshot)


async def test_rendered_hub_tool_schema_matches_snapshot(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    snapshot: SnapshotAssertion,
) -> None:
    """The hub mounts render a different surface from ``/mcp`` — a smaller tool
    set, tool descriptions written for a coding-agent client, and server
    instructions that tell it to call list_daimons first. The instructions are
    part of the snapshot because they are prompt text the model reads, and
    nothing else pins them."""
    mcp = build_hub_app(
        platform="discord",
        runtime=McpRuntime(
            session_factory=schema_sessionmaker,
            client=build_stub_anthropic(),
            settings=_fully_configured_settings(),
            deployment_default=DeploymentDefault(environment_name=None),
        ),
        auth=StaticTokenVerifier(tokens={}),
        billing_config=None,
    )
    tools = await mcp.local_provider.list_tools()

    rendered: dict[str, object] = {tool.name: _snapshot_value(tool) for tool in tools}
    assert rendered, "the hub tool registry must not be empty"
    assert mcp.instructions is not None, "the hub must tell clients to call list_daimons first"
    rendered["instructions"] = mcp.instructions.split("\n")
    _assert_each_matches_snapshot(rendered, snapshot)


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
    schema_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """create_environment takes one nested model, and the whole reason a
    prose docstring can be the complete fix for "the model never sets pip
    packages" is that this nested field reaches the model as a plain string
    array. If a dependency upgrade ever flattens it to an opaque object, this
    test goes red and the fix has to change shape — the snapshot alone would
    just be regenerated around the new (broken) shape."""
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=schema_sessionmaker,
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


async def test_the_tools_for_forking_onto_a_model_name_the_real_reason(sessionmaker) -> None:
    """J7: "fork yourself onto gpt-3.5-turbo" must never read as an admin/owner rule.

    The model only knows what these descriptions say. A copy can name the model the
    person asked for, so an unavailable one is refused as unavailable. A saved copy
    can subsequently switch its provider through the ordinary confirmation flow.
    """
    app = create_mcp_app(
        settings=_fully_configured_settings(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    tools = {tool.name: tool for tool in await app.state.mcp.local_provider.list_tools()}
    fork = tools["fork_agent"].description or ""
    assert "pass it as ``model``" in fork, fork
    assert "request a backend switch" in fork, fork
    assert not any("fleet" in name.lower() for name in tools)
    assert {
        "fork_agent",
        "request_provider_allowlist",
        "request_channel_pin",
        "request_channel_pin_clear",
    } <= tools.keys()

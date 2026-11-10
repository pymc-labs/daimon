"""Every surface reads and writes features through the registry, never by naming a key.

A new key is then reachable from chat, MCP, web and the CLI the moment it is registered,
which holds only while no surface names a key itself (the first test) and each goes
through the core service (the second). `_GAPS` records the deliberate differences with their
reasons, and the last test fails when one names a key or tier that is gone.

This file does not prove reachability end to end: that needs each surface driven for each
key, which the per-surface suites do (`test_settings_panel` lists every feature on each chat
platform within the select limit, `test_features_routes` the web schema, `test_features` the
CLI). `_GAPS` is therefore a record of intent, not a checked claim.
"""

from __future__ import annotations

import ast
from pathlib import Path

from daimon.core.features.registry import FEATURES, TIERS

ROOT = Path(__file__).resolve().parents[2]
_ADAPTERS = ROOT / "packages/adapters"
_SURFACES = {
    "discord": "discord/daimon/adapters/discord/commands/settings.py",
    "slack": "slack/daimon/adapters/slack/settings.py",
    "teams": "teams/daimon/adapters/teams/settings.py",
    "mcp": "mcp/daimon/adapters/mcp/tools/scope_settings.py",
    "web": "web/daimon/adapters/web/features.py",
    "cli": "cli/daimon/adapters/cli/commands/features.py",
}
#: Surface -> why it has no setter or reader for part of the registry. `*` is every key.
_GAPS: dict[tuple[str, str, str], str] = {
    ("cli", "approvals", "*"): "an approval needs a live chat admin; the CLI has none",
    ("slack", "*", "thread"): "a slash command carries no thread; set it on the web or in-thread",
    ("discord", "tool.*", "agent"): "the agent's own tool config is edited in the agent editor",
    ("slack", "tool.*", "agent"): "the agent's own tool config is edited in the agent editor",
    ("teams", "tool.*", "agent"): "the agent's own tool config is edited in the agent editor",
    ("mcp", "tool.*", "agent"): "the agent's own tool config is edited in the agent editor",
}


def _string_keys(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } & set(FEATURES)


def test_no_surface_names_a_feature_key() -> None:
    """A surface that lists keys would miss the next one; it reads the registry instead."""
    named = {name: _string_keys(_ADAPTERS / path) for name, path in _SURFACES.items()}
    assert not {name: keys for name, keys in named.items() if keys}


def test_every_surface_goes_through_the_core_feature_service() -> None:
    core = {"read_features", "change_feature", "change_setting", "open_panel", "press", "show"}
    for name, path in _SURFACES.items():
        tree = ast.parse((_ADAPTERS / path).read_text())
        used = {
            getattr(node.func, "id", None) or getattr(node.func, "attr", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert core & used, f"{name} must read and write through the core feature service"


def test_each_gap_names_a_real_key_and_tier() -> None:
    for (surface, key, tier), reason in _GAPS.items():
        assert surface in _SURFACES and reason
        assert key == "*" or key in FEATURES or any(k.startswith(key[:-1]) for k in FEATURES)
        assert tier == "*" or tier in TIERS

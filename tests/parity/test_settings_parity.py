"""The Settings group is one rule on every path: `/settings`, the agent tools and the web app.

Every path writes through `daimon.core.features.service.change_feature`; the stores behind the
group are written directly only by the paths in `_DIRECT_WRITERS`, each with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

from daimon.core.platform_words import WORDS
from daimon.core.stores.domain import CHAT_PLATFORMS

ROOT = Path(__file__).resolve().parents[2]
_ADAPTERS = ROOT / "packages/adapters"
_PATHS = {
    "discord": "discord/daimon/adapters/discord/commands/settings.py",
    "slack": "slack/daimon/adapters/slack/settings.py",
    "teams": "teams/daimon/adapters/teams/settings.py",
    "mcp": "mcp/daimon/adapters/mcp/tools/scope_settings.py",
    "web": "web/daimon/adapters/web/features.py",
}
_CORE_MODULES = {"daimon.core.features.service", "daimon.core.settings_panel"}
_DIRECT_WRITERS = {
    "mcp/daimon/adapters/mcp/tools/scope_settings.py": "`change_setting`, over the core service",
    "teams/daimon/adapters/teams/channel_settings.py": "the Teams channel dialog's own switches",
    "cli/daimon/adapters/cli/commands/features.py": "`daimon features set`, over the core service",
    "web/daimon/adapters/web/features.py": "`PUT /api/features`, over the core service",
}
#: Deliberate gap. A Slack slash command carries no thread, and a message shortcut needs an
#: entry in the app manifest, which lives outside this repo. A Slack thread is set on the web
#: (the thread page), by the agent's tool, or by asking Daimon in it.
_SLACK_HAS_NO_THREAD_LEVEL = True


def _names(tree: ast.AST) -> set[str]:
    """Every name imported from the core settings modules or called."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _CORE_MODULES:
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Call):
            found.add(getattr(node.func, "id", None) or getattr(node.func, "attr", ""))
    return found


def test_every_path_has_the_command_and_writes_through_the_core() -> None:
    assert all(WORDS[platform].command("settings") for platform in CHAT_PLATFORMS)
    for name, path in _PATHS.items():
        used = _names(ast.parse((_ADAPTERS / path).read_text()))
        core_writer = {"change_feature", "apply_choice", "press"} & used
        assert core_writer, f"{name} must write through daimon.core.features.service"


def test_store_writers_are_called_only_from_core_or_listed_paths() -> None:
    writers = {"set_approval_gate", "set_feature_value", "change_feature"}
    found = {
        path.relative_to(_ADAPTERS).as_posix()
        for path in _ADAPTERS.rglob("*.py")
        if "/tests/" not in path.as_posix() and _names(ast.parse(path.read_text())) & writers
    }
    assert not found - set(_DIRECT_WRITERS), "write through daimon.core.features.service"


def test_slack_settings_has_no_thread_level() -> None:
    source = (_ADAPTERS / _PATHS["slack"]).read_text()
    assert (
        _SLACK_HAS_NO_THREAD_LEVEL
        and 'FeaturePlace.of(actor.tenant_id, "slack", channel_id, None)' in source
    )

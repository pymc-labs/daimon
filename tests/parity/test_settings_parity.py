"""The Settings group is one rule on every path: `/settings`, the agent tools and the web app.

Every chat platform has a `settings` command, and every module that draws the panel or
changes a setting goes through `daimon.core.scope_settings` (and the chat panels through
`daimon.core.settings_card`). The stores behind the group are written directly only by
the paths listed in `_DIRECT_WRITERS`, each with its reason. Deliberate differences
between paths are in `_DELIBERATE_GAPS`, each checked against the code.
"""

from __future__ import annotations

import ast
from pathlib import Path

from daimon.core.platform_words import WORDS
from daimon.core.stores.domain import CHAT_PLATFORMS

ROOT = Path(__file__).resolve().parents[2]

#: The modules that implement the group on each path.
_PATHS: dict[str, str] = {
    "discord": "packages/adapters/discord/daimon/adapters/discord/commands/settings.py",
    "slack": "packages/adapters/slack/daimon/adapters/slack/settings.py",
    "teams": "packages/adapters/teams/daimon/adapters/teams/settings.py",
    "mcp": "packages/adapters/mcp/daimon/adapters/mcp/tools/scope_settings.py",
    "web": "packages/adapters/web/daimon/adapters/web/channel_settings.py",
}
_CHAT = ("discord", "slack", "teams")

#: Store writers behind the group.
_STORE_WRITERS = frozenset({"set_approval_gate", "set_switch", "set_cost_footer"})

#: Modules outside core that may still call a store writer directly, and why.
_DIRECT_WRITERS: dict[str, str] = {
    "packages/adapters/discord/daimon/adapters/discord/commands/here.py": (
        "the retired /here approvals menu, kept for one release; it records the change"
    ),
    "packages/adapters/slack/daimon/adapters/slack/here.py": (
        "the retired /here approvals menu, kept for one release; it records the change"
    ),
    "packages/adapters/teams/daimon/adapters/teams/here.py": (
        "the retired here approvals buttons, kept for one release; it records the change"
    ),
    "packages/adapters/mcp/daimon/adapters/mcp/tools/progress_plans.py": (
        "deprecated set_progress_plans, kept for one release"
    ),
    "packages/adapters/mcp/daimon/adapters/mcp/tools/thread_participation.py": (
        "deprecated set_thread_participation, kept for one release"
    ),
    "packages/adapters/web/daimon/adapters/web/channel_settings.py": (
        "the web switch routes may keep their existing writer; approvals and the cost "
        "footer go through change_setting"
    ),
    "packages/adapters/teams/daimon/adapters/teams/channel_settings.py": (
        "the Teams channel settings dialog's own switches"
    ),
}

#: Deliberate differences between paths, and the reason.
_DELIBERATE_GAPS: dict[tuple[str, str], str] = {
    ("slack", "no thread level in /settings"): (
        "Slack slash commands do not say which thread they were run in; a person sets "
        "a thread's own value by asking Daimon in that thread"
    ),
}


def _tree(path: str) -> ast.Module:
    return ast.parse((ROOT / path).read_text())


def _imported(tree: ast.Module, module: str) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def _called(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_every_chat_platform_has_a_settings_command() -> None:
    for platform in CHAT_PLATFORMS:
        assert WORDS[platform].command("settings") is not None, platform


def test_every_path_writes_through_the_core_service() -> None:
    for name, path in _PATHS.items():
        assert "change_setting" in _imported(_tree(path), "daimon.core.scope_settings"), name


def test_every_chat_panel_draws_the_shared_model() -> None:
    for name in _CHAT:
        imported = _imported(_tree(_PATHS[name]), "daimon.core.settings_card")
        assert {"build_settings_panel", "decode_choice"} <= imported, name


def test_store_writers_are_called_only_from_core_or_listed_paths() -> None:
    found: set[str] = set()
    for path in (ROOT / "packages/adapters").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel:
            continue
        if _called(ast.parse(path.read_text())) & _STORE_WRITERS:
            found.add(rel)
    unlisted = found - set(_DIRECT_WRITERS)
    assert not unlisted, f"write through daimon.core.scope_settings.change_setting: {unlisted}"


def test_slack_settings_has_no_thread_level() -> None:
    assert ("slack", "no thread level in /settings") in _DELIBERATE_GAPS
    for node in ast.walk(_tree(_PATHS["slack"])):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "of"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "SettingsPlace"
        ):
            third = node.args[2]
            assert isinstance(third, ast.Constant) and third.value is None

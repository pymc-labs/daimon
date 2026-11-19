"""Every path that opens a DM decides it by the agent's grant, with the person's groups.

Found by AST: a module that opens a DM (Discord `create_dm`, Slack
`conversations.open`, a Teams 1:1 chat) must check the grant
(`daimon.core.dm_access.sender_admits` or a caller of it), and every grant check
passes a real group lookup, so a grant that lists a role, user group or team
decides the same on every path. No database -- this reads the source.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = REPO_ROOT / "packages"

OPENERS = frozenset({"create_dm", "conversations_open", "open_dm", "open_chat", "open_member_dm"})
GRANT_CHECKS = frozenset({"sender_admits", "require_dm_grant", "permits_dm", "lookup_dm_groups"})

# Modules that open a DM without a grant check of their own, and why.
NO_GRANT_CHECK: dict[str, str] = {
    "adapters/teams/daimon/adapters/teams/private_replies.py": (
        "answers the person who asked, in their own chat; the turn's grant is checked there"
    ),
    "adapters/discord/daimon/adapters/discord/commands/direct_messages.py": (
        "the person's own cold DM or `/dm-me`; core `start_cold_dm` and `start_dm` check the grant"
    ),
    "adapters/discord/daimon/adapters/discord/bot.py": (
        "`open_member_dm` opens the chat routine delivery asked for after its grant check"
    ),
}


def _sources() -> dict[str, ast.Module]:
    found: dict[str, ast.Module] = {}
    for path in sorted(PACKAGES.glob("*/**/daimon/**/*.py")):
        if "tests" not in path.parts:
            found[path.relative_to(PACKAGES).as_posix()] = ast.parse(path.read_text())
    return found


def _name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _calls(tree: ast.AST, names: frozenset[str]) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _name(n) in names]


SOURCES = _sources()


def test_every_module_that_opens_a_dm_checks_the_agents_grant() -> None:
    openers = {path for path, tree in SOURCES.items() if _calls(tree, OPENERS)}
    assert len(openers) >= 10, "the AST still finds the DM paths"
    unchecked = {path for path in openers if not _calls(SOURCES[path], GRANT_CHECKS)}
    assert unchecked == set(NO_GRANT_CHECK), "a new DM path checks the grant, or says why not"


def test_every_grant_check_passes_a_group_lookup() -> None:
    missing = [
        f"{path}:{call.lineno} {_name(call)}"
        for path, tree in SOURCES.items()
        for call in _calls(tree, GRANT_CHECKS)
        if not any(
            k.arg == "groups" and not (isinstance(k.value, ast.Constant) and k.value.value is None)
            for k in call.keywords
        )
    ]
    assert missing == [], "a grant listing a group would refuse the people in it"


def test_slack_and_teams_dm_switches_pass_the_switchers_groups() -> None:
    switches = {
        path: _calls(tree, frozenset({"switch_thread_on_request"}))
        for path, tree in SOURCES.items()
        if path.startswith(("adapters/slack/", "adapters/teams/"))
    }
    calls = [(path, call) for path, found in switches.items() for call in found]
    assert {path.split("/")[1] for path, _ in calls} == {"slack", "teams"}
    for path, call in calls:
        assert any(k.arg == "dm_groups" for k in call.keywords), f"{path}:{call.lineno}"

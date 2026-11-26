"""The deprecated env DM policy is one rule, applied on every DM path with the stored access.

Found by AST: only `config.env_dm_policy` picks a workspace's env policy, so its default
lives in one place; every module that opens a DM or checks a grant applies it (through
`env_admits`, `env_dm_policy` or a check that does, like `decide_dm`); and every decision
gets the settings it reads the policy from. No database -- this reads the source.
"""

from __future__ import annotations

import ast

from .test_dm_group_lookups import GRANT_CHECKS, OPENERS, SOURCES

DECISIONS = frozenset({"decide_dm", "permits_dm", "explain_dm"})
"""Core decisions that apply the env policy themselves, given the settings."""
ENV_CHECKS = frozenset(
    {
        "env_admits",
        "env_dm_policy",
        "allowed_recipients",
        "require_dm_grant",
        "sharing_refusal",  # every secret-share card's check (`core/secret_share_flow.py`)
        *DECISIONS,
    }
)

# Modules that open a DM or check a grant without the env policy, and why.
NO_ENV_CHECK: dict[str, str] = {
    "adapters/teams/daimon/adapters/teams/private_replies.py": (
        "answers the person who asked, in their own chat; the turn's admission applied it"
    ),
}
# Decisions made without settings, and why.
NO_SETTINGS: dict[str, str] = {
    "core/daimon/core/thread_handoff.py": (
        "moves a DM thread the person already has, which the env policy admitted"
    ),
}


def _name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _calls(tree: ast.AST, names: frozenset[str]) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _name(n) in names]


def _applies(tree: ast.AST) -> bool:
    if _calls(tree, ENV_CHECKS):
        return True
    # A delivery handed the policy by its caller calls `.allows` on it.
    return any(
        isinstance(c.func, ast.Attribute) and "dm_policy" in ast.unparse(c.func.value)
        for c in _calls(tree, frozenset({"allows"}))
    )


def test_only_the_helper_picks_a_workspaces_env_policy() -> None:
    pickers = {
        path for path, tree in SOURCES.items() if _calls(tree, frozenset({"DirectMessagePolicy"}))
    }
    assert pickers == {"core/daimon/core/config.py"}, "use config.env_dm_policy or env_admits"


def test_every_dm_path_applies_the_env_policy() -> None:
    paths = {path for path, tree in SOURCES.items() if _calls(tree, OPENERS | GRANT_CHECKS)}
    assert len(paths) >= 15, "the AST still finds the DM paths"
    missing = {path for path in paths if not _applies(SOURCES[path])}
    assert missing == set(NO_ENV_CHECK), "a new DM path applies the env policy, or says why not"


def test_every_dm_decision_gets_the_settings_it_reads_the_policy_from() -> None:
    blind = {
        path
        for path, tree in SOURCES.items()
        for call in _calls(tree, DECISIONS)
        if len(call.args) < 2
        or (isinstance(call.args[1], ast.Constant) and call.args[1].value is None)
    }
    assert blind == set(NO_SETTINGS), "pass the settings, or say why not"

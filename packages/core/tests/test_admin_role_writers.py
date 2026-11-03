"""Platform snapshots have a small, explicit set of writers."""

import ast
from pathlib import Path


def test_only_platform_turns_and_github_commands_can_write_role_snapshots() -> None:
    root = Path(__file__).resolve().parents[2]
    writers: set[str] = set()
    for path in root.rglob("*.py"):
        if "daimon" not in path.parts or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        aliases = {"set_role"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "daimon.core.stores.accounts":
                aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "set_role"
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name in aliases:
                writers.add(str(path.relative_to(root)))
    assert writers == {
        "core/daimon/core/turn/admission.py",
        "adapters/discord/daimon/adapters/discord/commands/github.py",
        "adapters/slack/daimon/adapters/slack/github_connect.py",
    }

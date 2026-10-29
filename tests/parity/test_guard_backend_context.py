"""Every production backend guard receives an explicit thread and caller context."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD_SITES = {
    "packages/core/daimon/core/thread_handoff.py",
    "packages/core/daimon/core/continuity/dispatch.py",
    "packages/core/daimon/core/continuity/watch_reports.py",
    "packages/adapters/discord/daimon/adapters/discord/bot.py",
    "packages/adapters/discord/daimon/adapters/discord/commands/here.py",
    "packages/adapters/slack/daimon/adapters/slack/app.py",
    "packages/adapters/slack/daimon/adapters/slack/here.py",
    "packages/adapters/teams/daimon/adapters/teams/app.py",
    "packages/adapters/teams/daimon/adapters/teams/here.py",
}


def test_every_backend_guard_passes_thread_and_caller_context() -> None:
    sites: list[str] = []
    for path in (ROOT / "packages").rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        names = {"guard_selfhosted_channel"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "guard_selfhosted_channel"
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
            if name not in names:
                continue
            relative = path.relative_to(ROOT).as_posix()
            sites.append(relative)
            keywords = {kw.arg: kw.value for kw in node.keywords}
            assert {"thread_id", "caller_id"} <= keywords.keys(), (
                f"{relative}:{node.lineno} must pass thread and caller identity"
            )
            # Slack slash commands have no thread context in the platform payload.
            for key in ("thread_id", "caller_id"):
                value = keywords[key]
                if isinstance(value, ast.Constant) and value.value is None:
                    assert key == "thread_id" and relative.endswith("slack/here.py")
    assert set(sites) == GUARD_SITES, "Review and enumerate every new guard entry point"
    assert len(sites) == len(GUARD_SITES), "Review additional guards within an existing entry point"

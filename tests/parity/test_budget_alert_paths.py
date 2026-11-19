"""Alert-only budgets alert from one place: each chat adapter's poller over the ledger, by AST."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAT = ("discord", "slack", "teams")

NO_POLLER = {
    "cli": "no platform client; the budget's chat adapter posts its alerts",
    "mcp": "no platform client; the budget's chat adapter posts its alerts",
    "scheduler": "no platform client; the budget's chat adapter posts its alerts",
    "sidecar": "no platform client; its watch reads spend through the ledger too",
    "web": "no platform client; the budget's chat adapter posts its alerts",
}
"""Spend paths that run no poller, with why: the poller reads their spend from the ledger."""


def _calls(package: Path, name: str) -> bool:
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name:
                return True
    return False


@pytest.mark.parametrize("platform", CHAT)
def test_alerts_when_a_chat_adapter_runs_then_it_polls_the_ledger(platform: str) -> None:
    package = ROOT / f"packages/adapters/{platform}/daimon/adapters/{platform}"
    assert _calls(package, "run_budget_alert_poller"), f"{platform} never posts budget alerts"


def test_alerts_when_spend_lands_off_chat_then_the_reason_is_listed() -> None:
    adapters = {p.name for p in (ROOT / "packages/adapters").iterdir() if (p / "daimon").is_dir()}
    assert adapters - set(CHAT) == set(NO_POLLER), "list each non-chat adapter with its reason"
    turn = ROOT / "packages/core/daimon/core/turn"
    assert not _calls(turn, "notify_budget_alert"), "admission must not alert: it misses spend"

"""Ask a person reaches every chat path through one core helper, found by AST.

Each path is listed with what it does.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAT = ("discord", "slack", "teams")

OFFERS = {
    "discord/feedback_reactions.py": "the reaction under an answer",
    "discord/support_escalation.py": "the Ask a person button",
    "slack/support_escalation.py": "the Ask a person button",
    "teams/support.py": "the Ask a person card",
}
"""Every path that offers the menu, so each refuses through `offer_refusal`."""

READS_LIMIT_ONLY: dict[str, str] = {}
"""A path that reads a limit yet offers no menu, with why."""

DEPLOYMENT_READS = frozenset(
    {
        "escalation_channel_id",
        "slack_escalation_channel_id",
        "teams_escalation_channel_id",
        "routes_feedback",
        "feedback_to_support",
    }
)


def _source(platform: str) -> Path:
    return ROOT / f"packages/adapters/{platform}/daimon/adapters/{platform}"


def _trees(platform: str) -> dict[str, ast.Module]:
    base = _source(platform)
    return {
        f"{platform}/{path.relative_to(base)}": ast.parse(path.read_text())
        for path in sorted(base.rglob("*.py"))
    }


def _name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _calls(helper: str, platforms: tuple[str, ...] = CHAT) -> dict[str, list[ast.Call]]:
    """Every call to `helper` in these adapters' sources, by `platform/module.py`."""
    found: dict[str, list[ast.Call]] = {}
    for platform in platforms:
        for module, tree in _trees(platform).items():
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _name(n) == helper]
            if calls:
                found[module] = calls
    return found


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def test_offer_refusal_when_a_path_offers_the_menu_then_it_refuses_through_core() -> None:
    offering = set(_calls("offer_refusal"))
    assert offering == set(OFFERS), "the listed paths, and no unlisted one"
    reading = set(_calls("resolve_limit")) - set(READS_LIMIT_ONLY)
    assert reading <= offering, f"reads a limit, offers without offer_refusal: {reading - offering}"


@pytest.mark.parametrize("platform", CHAT)
def test_settle_when_clicked_then_the_post_checks_the_stamped_channel(platform: str) -> None:
    """`mark_delivered` stamps where a request went; `settle_from_post` checks a click there."""
    stamps = [c for calls in _calls("mark_delivered", (platform,)).values() for c in calls]
    assert stamps and all(_keyword(c, "support_channel_id") for c in stamps), "stamped"
    settles = [c for calls in _calls("settle_from_post", (platform,)).values() for c in calls]
    assert settles, "the post's settle goes through core"
    for call in settles:
        for kw in ("channel_id", "parent_id", "default_channel_id"):
            assert _keyword(call, kw) is not None, f"settle_from_post gets {kw}"


ROUTED = [
    pytest.param(helper, platform, what, id=f"{helper}-{platform}")
    for helper, what in (
        ("support_destination", "where a request is posted"),
        ("feedback_destination", "where a thumbs-down form also goes"),
        ("deployment_channel", "the default channel a settle falls back to"),
    )
    for platform in CHAT
]


@pytest.mark.parametrize(("helper", "platform", "what"), ROUTED)
def test_routing_when_a_path_picks_a_channel_then_core_picks_it(
    helper: str, platform: str, what: str
) -> None:
    assert _calls(helper, (platform,)), f"{platform}: {what} comes from support_routing.{helper}"


@pytest.mark.parametrize("platform", CHAT)
def test_routing_when_a_chat_adapter_runs_then_it_reads_no_channel_setting(platform: str) -> None:
    """A wrong-platform id, an own channel, and the feature are core's; never re-read here."""
    reads = {
        module
        for module, tree in _trees(platform).items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in DEPLOYMENT_READS
    }
    assert not reads, f"{platform} reads Ask a person's settings directly: {sorted(reads)}"

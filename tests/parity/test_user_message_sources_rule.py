"""Rule contract: every user message sent to a provider decides the web search sources rule.

The rule (`daimon.core.turn.web_search_sources`) rides in front of the user
message, so each place that hands one on must decide it. This walks the code of
every package for a call that passes a user message (`user_message=`, a reseed
or a continuation seed) and for every `user.message` event built by hand. Each
must sit in a function that decides the rule, call a send that does, or be
listed below with why it needs none.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

_PACKAGES = Path(__file__).resolve().parents[2] / "packages"

#: Source text of a function that decides the rule.
_RULE = re.compile(r"\bsources_prefix\(|\b_web_search_sources\(|\+\s*sources\b")

#: Keywords that carry a user message to a send.
_MESSAGE_KEYWORDS = frozenset({"user_message", "reseed_user_message", "seed_user_message"})

#: Callees that send through a function deciding the rule, so a call needs none.
_FUNNELS: dict[str, str] = {
    "run_prepared_turn": "the chat turn: its main and reseed sends decide the rule",
    "run_prepared_turn_impl": "the chat turn's body, where both sends decide the rule",
    "_with_handoff_summary": "frames the chat turn's message before its sends",
    "_with_agent_notices": "frames the chat turn's message before its sends",
    "ContinuationDecision": "a wake or continuation seed, sent as a chat turn",
}

#: (file under packages/, function): a send that decides no rule, and why.
_NO_RULE: dict[tuple[str, str], str] = {
    ("adapters/cli/daimon/adapters/cli/run/command.py", "run_conversation"): (
        "forwards the message `_run_with_defaults` framed with the rule"
    ),
    ("adapters/cli/daimon/adapters/cli/run/command.py", "run_conversation_observed"): (
        "forwards the message `_run_with_defaults` framed with the rule"
    ),
    ("core/daimon/core/turn/driver.py", "run_turn._send_initial"): (
        "the driver sends the message its caller framed"
    ),
    ("core/daimon/core/long_jobs_ma.py", "drive"): (
        "a fixed shell protocol whose answer is a manifest, not a reply"
    ),
    ("core/daimon/core/workspace_transfer.py", "transfer_workspace"): (
        "the checkpoint gate turns built-ins, search included, off"
    ),
    ("core/daimon/core/turn/openai_codec.py", "display_event"): "renders a stored event",
    ("core/daimon/core/turn/selfhosted_codec.py", "display_event"): "renders a stored event",
}

#: Sends the walk must reach: agent chat, the scheduler, wakes and continuations.
_MUST_REACH = frozenset(
    {
        ("adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py", "_send_billed"),
        ("core/daimon/core/headless_runner.py", "run_turn_impl"),
        ("core/daimon/core/continuity/continuation.py", "decide_continuation"),
        ("adapters/discord/daimon/adapters/discord/bot.py", "_run_continuation_turn_observed"),
        ("adapters/slack/daimon/adapters/slack/app.py", "_run_continuation_turn_observed"),
        ("core/daimon/core/turn/run.py", "run_prepared_turn_impl._run"),
    }
)


class _Site(NamedTuple):
    file: str
    function: str
    callee: str
    decides: bool


def _sources() -> list[Path]:
    """Every package's own code. Mux only translates a message it was handed."""
    skip = ("tests", "testing", "mux")
    return [
        path
        for path in sorted(_PACKAGES.rglob("*.py"))
        if not any(part in skip for part in path.relative_to(_PACKAGES).parts)
    ]


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "?")


def _is_user_message_event(node: ast.Dict) -> bool:
    return any(
        isinstance(key, ast.Constant)
        and key.value == "type"
        and isinstance(value, ast.Constant)
        and value.value == "user.message"
        for key, value in zip(node.keys, node.values, strict=True)
    )


def _sites() -> list[_Site]:
    """Each user message send, with whether its top-level function decides the rule."""
    found: list[_Site] = []
    for path in _sources():
        text = path.read_text()
        file = str(path.relative_to(_PACKAGES))
        for top in ast.parse(text).body:
            if not isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            decides = bool(_RULE.search(ast.get_source_segment(text, top) or ""))
            found.extend(_walk(top, file, [], decides=decides))
    return found


def _walk(node: ast.AST, file: str, names: list[str], *, decides: bool) -> list[_Site]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        names = [*names, node.name]
    function = ".".join(names)
    found: list[_Site] = []
    if isinstance(node, ast.Call) and any(k.arg in _MESSAGE_KEYWORDS for k in node.keywords):
        found.append(_Site(file, function, _callee(node), decides))
    if isinstance(node, ast.Dict) and _is_user_message_event(node):
        found.append(_Site(file, function, "user.message event", decides))
    for child in ast.iter_child_nodes(node):
        found.extend(_walk(child, file, names, decides=decides))
    return found


def test_every_user_message_send_decides_the_sources_rule() -> None:
    """A send that skips the rule fails unless it reaches one that decides it or is listed."""
    undecided = [
        f"{site.file}::{site.function} -> {site.callee}"
        for site in _sites()
        if not site.decides
        and site.callee not in _FUNNELS
        and (site.file, site.function) not in _NO_RULE
    ]
    assert not undecided, (
        f"these sends skip the sources rule: {undecided}. Decide it with `sources_prefix`, "
        "or list the send in _NO_RULE with why it needs none"
    )


def test_the_walk_reaches_every_known_send() -> None:
    """The walk finds agent chat, the scheduler, wakes and continuations, and every listed send."""
    sites = _sites()
    places = {(site.file, site.function) for site in sites}
    callees = {site.callee for site in sites}
    assert places >= _MUST_REACH, f"the walk misses {sorted(_MUST_REACH - places)}"
    assert set(_NO_RULE) <= places, f"stale _NO_RULE entries: {sorted(set(_NO_RULE) - places)}"
    assert set(_FUNNELS) <= callees, f"stale _FUNNELS entries: {sorted(set(_FUNNELS) - callees)}"


def test_agent_chat_and_the_scheduler_decide_the_rule_themselves() -> None:
    """The headless sends decide the rule in their own code, not through an allowance."""
    own = {
        (site.file, site.function)
        for site in _sites()
        if site.decides and (site.file, site.function) in _MUST_REACH
    }
    for place in (
        ("adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py", "_send_billed"),
        ("core/daimon/core/headless_runner.py", "run_turn_impl"),
    ):
        assert place in own, f"{place[1]} must decide the sources rule itself"

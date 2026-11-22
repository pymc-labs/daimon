"""Every chat message handler checks a workflow ask's answer, then a mention trigger, before it
admits a turn.

`daimon.core.workflows.answers.claim_reply` decides; each adapter's `workflow_asks`
claim runs it. In every listed handler the claim is an `if` that only returns (or, in a
restart's catch-up loop, moves on to the next message), ahead of
the call that starts admission, so a message answering no ask goes on as ordinary chat
(each adapter's `test_workflow_asks` shows chat in another thread is not claimed).
`daimon.core.workflows.mentions.claim_mention` comes next, the same way, in every handler but
the DM ones (`DMS`): a direct message tags no one in a channel a trigger listens to. A
restart's replay (Discord) and catch-up (Teams) claim it too: the mention event's message id
dedupes a message a live handler already claimed (`test_mentions.py`). Discord's replay skips
the ask claim only.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

_ADAPTERS = Path(__file__).parents[2] / "packages" / "adapters"
_CLAIMS = ("claim_message", "claim_event", "claim_inbound")
#: (file, handler, the call that starts admission), one per inbound message path.
HANDLERS = [
    ("discord/daimon/adapters/discord/bot.py", "on_message", "discord_message_admissions"),
    ("discord/daimon/adapters/discord/commands/direct_messages.py", "on_message", "bursts.join"),
    ("slack/daimon/adapters/slack/app.py", "_handle_app_mention", "protection_state"),
    ("slack/daimon/adapters/slack/direct_messages.py", "handle_direct_message", "bursts.join"),
    ("teams/daimon/adapters/teams/app.py", "_answer_or_admit", "_admit"),
    ("teams/daimon/adapters/teams/app.py", "catch_up_graph_messages", "_claim"),
]
#: Direct-message handlers: no mention trigger listens to a DM.
DMS = {"bursts.join"}


def _origins(tree: ast.Module) -> dict[str, str]:
    """Each imported name's module, so `claim_message` reads as where it came from."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return found


def _name(call: ast.Call, origins: dict[str, str]) -> str:
    head, _, rest = ast.unparse(call.func).partition(".")
    return origins.get(head, head) + (f".{rest}" if rest else "")


def _called(node: ast.AST, origins: dict[str, str]) -> list[str]:
    return [_name(c, origins) for c in ast.walk(node) if isinstance(c, ast.Call)]


def _is_claim(name: str) -> bool:
    return any(name.endswith(f"workflow_asks.{c}") for c in _CLAIMS)


def _is_mention(name: str) -> bool:
    return name.endswith("workflow_cards.claim_mention")


def _gate(func: ast.AST, origins: dict[str, str], is_claim: Callable[[str], bool]) -> ast.If:
    """The one `if` that claims the message and only returns or moves on."""
    claims = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.If) and any(is_claim(c) for c in _called(n.test, origins))
    ]
    assert len(claims) == 1, f"{len(claims)} claims"
    [claim] = claims
    ends = isinstance(claim.body[-1], ast.Return | ast.Continue)
    assert ends and not claim.orelse, ast.unparse(claim)
    return claim


@pytest.mark.parametrize(("path", "handler", "admits"), HANDLERS)
def test_the_ask_claim_returns_before_admission(path: str, handler: str, admits: str) -> None:
    tree = ast.parse((_ADAPTERS / path).read_text())
    origins = _origins(tree)
    [func] = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == handler
    ]
    claim = _gate(func, origins, _is_claim)
    later = [
        call.lineno
        for call in ast.walk(func)
        if isinstance(call, ast.Call) and admits in _name(call, origins)
    ]
    assert later and claim.lineno < min(later), f"{path}:{handler} admits before the claim"
    if admits in DMS:
        return
    mention = _gate(func, origins, _is_mention)
    assert claim.lineno < mention.lineno < min(later), f"{path}:{handler} misorders the mention"


@pytest.mark.parametrize(
    ("path", "handler"),
    [
        ("discord/daimon/adapters/discord/bot.py", "on_message"),
        ("teams/daimon/adapters/teams/app.py", "catch_up_graph_messages"),
    ],
)
def test_a_restart_replay_claims_mention_triggers_like_a_live_message(
    path: str, handler: str
) -> None:
    tree = ast.parse((_ADAPTERS / path).read_text())
    [func] = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == handler
    ]
    gate = _gate(func, _origins(tree), _is_mention)
    names = {n.id for n in ast.walk(gate.test) if isinstance(n, ast.Name)}
    assert "startup_replay" not in names, f"{path}:{handler} skips mentions on a replay"

"""Every place that gives a turn own-DM reach, found from the code.

Own-DM reach lets a turn read a limited room its person can read themselves. It is set at
admission and only on an owner's own DM turn: the live reply and the scheduler's owner-DM
routine. A watch, a timer, a channel timer, a helper, a child session, the web app, a
relay, agent chat and a self-hosted turn never pass it, so they default to none.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]

#: (file, enclosing function, value) of every `own_dm_reach=` keyword in the source, with why.
EXPECTED = {
    # The live DM reply: Discord only, the platform of the reach rule.
    ("core/daimon/core/direct_messages.py", "reply_to_dm_result", "platform == 'discord'"),
    # The scheduler's owner-DM routine, the one fire that is the owner's own routine.
    ("core/daimon/core/direct_messages.py", "run_dm_routine", "True"),
    # The shared DM fire: a watch or a timer on this path is cut off here.
    (
        "core/daimon/core/direct_messages.py",
        "_run_dm_fire",
        "own_dm_reach and watch_row is None and (timer_row is None)",
    ),
    # Plumbing: each passes on what its caller decided.
    ("core/daimon/core/direct_messages.py", "_run_unattended_turn", "own_dm_reach"),
    ("core/daimon/core/direct_messages.py", "_run_admitted_dm_turn", "own_dm_reach"),
    ("core/daimon/core/turn_origin.py", "turn_origin", "own_dm_reach"),
    ("core/daimon/core/stores/turn_origins.py", "create_origin", "own_dm_reach"),
}


def _uses() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path in PACKAGES.rglob("*.py"):
        parts = path.relative_to(PACKAGES).parts
        if "tests" in parts or ".venv" in parts or "alembic" in parts:
            continue
        source = path.read_text()
        if "own_dm_reach" not in source:
            continue
        tree = ast.parse(source)
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.keyword) and node.arg == "own_dm_reach":
                    found.add(("/".join(parts), function.name, ast.unparse(node.value)))
    return found


def test_only_an_owners_own_dm_turn_is_given_own_dm_reach() -> None:
    found = _uses()

    assert found == EXPECTED, f"a path gives a turn own-DM reach, or one was lost: {sorted(found)}"


def test_the_capability_defaults_to_none_for_every_other_origin() -> None:
    from daimon.core.stores.domain import TurnOriginRow

    assert TurnOriginRow.model_fields["own_dm_reach"].default is False
    assert TurnOriginRow.model_fields["reach_read"].default is False

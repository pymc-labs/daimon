"""Executable record: the termination notice is drawn by the three chat adapters.

Each reads `TurnState.termination` in `on_terminal_failure` and renders the core
`TerminationNotice` in its own markup, so the copy is shared and only the
drawing differs. No lifecycle hook was added for it: a surface that does not
draw the notice (CLI, headless routines, a new adapter) still gets the reason
on the state and on `RunOutcome.termination`, and `TerminationNotice.plain_text`
is there when it wants words. If another lifecycle starts drawing it, extend
this record rather than deleting it.
"""

from __future__ import annotations

import inspect
from types import ModuleType

import daimon.adapters.cli.run.lifecycle as cli_lifecycle
import daimon.adapters.discord.lifecycle as discord_lifecycle
import daimon.adapters.slack.lifecycle as slack_lifecycle
import daimon.adapters.teams.lifecycle as teams_lifecycle
import daimon.core.headless_runner as headless_runner


def _draws_notice(module: ModuleType) -> bool:
    return "render_termination_notice" in inspect.getsource(module)


def test_only_the_chat_adapters_draw_the_termination_notice() -> None:
    assert all(map(_draws_notice, (discord_lifecycle, slack_lifecycle, teams_lifecycle))), (
        "Discord, Slack and Teams explain an early end on the turn card"
    )
    assert not _draws_notice(headless_runner) and not _draws_notice(cli_lifecycle)

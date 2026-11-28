"""Workflow cards and `/workflows` in chat: every platform has them; the gaps are recorded.

Each gap below names why it exists and is asserted, so closing one fails this test until
its line goes:

- Teams cannot run a confirmation card: an invoke carries no card text to read the shown
  inputs back from, so Run there answers that the workflow changed.
- Teams answers on the pressed card: a Test run, a run form or a run card replaces it.
- A draft card is not refreshed in place: each check posts a new card, and an older card's
  Test run and Publish are refused once the draft moves on.
- A Discord run form holds 5 fields (Discord's modal limit), Slack and Teams 10; more
  fields send the person to the web app.
- Publishing from chat records no note.
- A titled post finds its Discord thread by name too (adopting one a routine made); Slack
  and Teams threads are found by record only, as they are a titled post and its replies.
- A `role:` mention is named, never pinged (role and group pings stay off); Teams names
  no role at all, and a Teams post carries no link back to it.
- An ask posted into a thread the run did not open takes only replies to it. Slack and
  Teams messages carry no reply reference, so there it is answered by its buttons only.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from daimon.adapters.discord import workflow_cards as discord_cards
from daimon.adapters.discord import workflow_delivery as discord_posts
from daimon.adapters.discord.commands.workflows import WorkflowsCog
from daimon.adapters.slack import workflow_asks as slack_asks
from daimon.adapters.slack import workflow_cards as slack_cards
from daimon.adapters.slack import workflow_delivery as slack_posts
from daimon.adapters.teams import workflow_asks as teams_asks
from daimon.adapters.teams import workflow_cards as teams_cards
from daimon.adapters.teams import workflow_delivery as teams_posts
from daimon.core.platform_words import WORDS
from daimon.core.workflows import chat_cards
from daimon.core.workflows import delivery as workflow_delivery

ROOT = Path(__file__).resolve().parents[2]


def test_every_chat_platform_has_the_workflows_panel() -> None:
    for words in WORDS.values():
        assert "workflows" in words.commands, words.platform
    names = [c.name for c in WorkflowsCog(object()).walk_app_commands()]  # type: ignore[arg-type]
    assert names == ["workflows"], "Discord registers /workflows"
    slack = (ROOT / "packages/adapters/slack/daimon/adapters/slack/app.py").read_text()
    assert 'cmd == "/workflows"' in slack, "Slack dispatches /workflows"
    teams = (ROOT / "packages/adapters/teams/daimon/adapters/teams/http_service.py").read_text()
    assert '"workflows": functools.partial(workflow_cards.command' in teams


def test_the_recorded_gaps_still_hold() -> None:
    assert "card_text=" not in inspect.getsource(teams_cards.answer), "Teams reads no card text"
    assert "open_pressed" not in inspect.getsource(teams_cards), "Teams answers in place"
    assert (discord_cards.FORM_LIMIT, slack_cards.FORM_LIMIT, teams_cards.FORM_LIMIT) == (5, 10, 10)
    publish = inspect.getsource(chat_cards._publish)  # pyright: ignore[reportPrivateUsage]
    assert 'source="chat"' in publish and 'wid, "",' in publish, "no note from chat"
    assert chat_cards.STALE, "an older draft card is refused, not refreshed"


def test_the_recorded_post_gaps_still_hold() -> None:
    discord, slack, teams = (
        inspect.getsource(m) for m in (discord_posts, slack_posts, teams_posts)
    )
    assert "find=find" in discord and "find=" not in slack + teams, "threads by name: Discord"
    tags = inspect.getsource(workflow_delivery._mention_tags)  # pyright: ignore[reportPrivateUsage]
    assert "<@&" in tags and "<!subteam^" in tags and "roles=False" in discord, "roles named"
    assert '"link": None' in teams, "a Teams post has no link"
    replies = inspect.getsource(slack_asks) + inspect.getsource(teams_asks)
    assert "reply_to=" not in replies, "Slack and Teams replies carry no reply reference"

"""Rule contract: a routine moved to Workflows is read-only on every chat panel.

Each panel reads the move (`migrated_workflows`), says so (`MOVED_TO_WORKFLOWS`) and
offers no pause, resume or delete; the workflow link is a button on Discord and Teams
and the moved line itself on Slack, whose row accessory holds one element.
"""

from __future__ import annotations

import ast
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
from daimon.adapters.discord.routines_panel import panel as discord_panel
from daimon.adapters.discord.routines_panel import state as discord_state
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.slack.routines_panel import state as slack_state
from daimon.adapters.slack.routines_panel.views import build_content_view
from daimon.adapters.teams import routines_card
from daimon.core.routines import MOVED_TO_WORKFLOWS
from daimon.core.stores.domain import RoutineRow
from daimon.core.web_links import WebLinks

_ADAPTERS = Path(__file__).resolve().parents[2] / "packages" / "adapters"
_READERS = (
    "discord/daimon/adapters/discord/routines_panel/read.py",
    "slack/daimon/adapters/slack/routines_panel/read.py",
    "teams/daimon/adapters/teams/routines_panel.py",
)
#: Where each panel's pause and resume buttons land; a card shown before the move still has them.
_ACTIONS = (
    "discord/daimon/adapters/discord/routines_panel/panel.py",
    "slack/daimon/adapters/slack/routines_panel/actions.py",
    "teams/daimon/adapters/teams/routines_panel.py",
)
WORKFLOW = uuid.uuid4()
LINKS = WebLinks("https://app.example.com", frozenset({"routines", "workflows"})).for_tenant(
    uuid.uuid4(), admin=True
)
URL = LINKS.workflow(WORKFLOW)
NOW = datetime(2026, 1, 1, tzinfo=UTC)
ROW = RoutineRow(
    id=uuid.uuid4(),
    tenant_id=uuid.uuid4(),
    created_by_user_id="u",
    agent_id="agent_a",
    agent_name="Alpha",
    cron_expr="0 9 * * 1-5",
    timezone="UTC",
    trigger_message="summarize",
    enabled=False,
    next_fire_at=None,
    last_fired_at=None,
    last_error=None,
    last_result_tail=None,
    created_at=NOW,
    updated_at=NOW,
)
CHANGES = {"Pause", "Resume", "Delete"}


def test_every_panel_reads_the_move() -> None:
    for path in _READERS:
        tree = ast.parse((_ADAPTERS / path).read_text())
        names = {
            n.attr if isinstance(n, ast.Attribute) else n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name | ast.Attribute)
        }
        assert "migrated_workflows" in names, f"{path} must read which routines moved"


def test_every_stale_pause_or_resume_says_the_routine_moved() -> None:
    for path in _ACTIONS:
        source = (_ADAPTERS / path).read_text()
        assert "apply_routine_action(" in source and "MOVED_TO_WORKFLOWS" in source, path


def test_discord_shows_the_move_and_links_the_workflow() -> None:
    entry = discord_state.RoutineEntry(ROW, "Alpha", "⏸", 0, "summarize", workflow_id=WORKFLOW)
    state = discord_state.RoutinesPanelState.initial(
        rows=[entry], over_cap_count=0, agent_name_map={}, links=LINKS
    )
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.turn_deps = SimpleNamespace(membership_resolver=None)
    view = discord_panel.RoutinesPanelView(state, runtime=runtime, allowed_user_id=1)
    items = list(view.walk_children())
    texts = " ".join(i.content for i in items if isinstance(i, discord.ui.TextDisplay))
    buttons = [i for i in items if isinstance(i, discord.ui.Button)]
    assert MOVED_TO_WORKFLOWS in texts
    assert all(b.disabled for b in buttons if str(b.label) in CHANGES)
    assert URL in {b.url for b in buttons}


def test_slack_shows_the_move_and_links_the_workflow() -> None:
    entry = slack_state.RoutineEntry(ROW, "Alpha", "⏸", "summarize", workflow_id=WORKFLOW)
    state = slack_state.RoutinesPanelState([entry], 0, {}, links=LINKS)
    row = build_content_view(state)["blocks"][1]
    assert f"<{URL}|{MOVED_TO_WORKFLOWS}>" in row["text"]["text"]
    assert [o["value"] for o in row["accessory"]["options"]] == ["output"]


def test_teams_shows_the_move_and_links_the_workflow() -> None:
    card = routines_card.panel_card([ROW], 0, user_id="u", is_admin=True, moved={ROW.id: URL})
    dumped = json.dumps(card.model_dump(by_alias=True))
    assert MOVED_TO_WORKFLOWS in dumped and URL is not None and URL in dumped
    assert not any(f'"title": "{change}"' in dumped for change in CHANGES)

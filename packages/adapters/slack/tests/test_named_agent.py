"""The Slack mention path passes only the explicit name form to admission."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.app import SlackApp
from daimon.core.turn.errors import NamedAgentRefused
from slack_sdk.web.async_client import AsyncWebClient


async def test_slack_passes_named_mention_to_admit() -> None:
    app = object.__new__(SlackApp)
    app.runtime = MagicMock()
    app._bot_user_ids = {"T": "B"}
    web_client = MagicMock(spec=AsyncWebClient)
    web_client.chat_postMessage = AsyncMock()
    with (
        patch("daimon.adapters.slack.app.resolve_admin_status", new_callable=AsyncMock) as admin,
        patch("daimon.adapters.slack.app.admit", new_callable=AsyncMock) as admit,
    ):
        admin.return_value = True
        admit.side_effect = NamedAgentRefused(
            kind="thread",
            current_name="Daimon",
            named_name="Planner",
            hand_over_agent_id="ag_planner",
            hand_over_agent_name="Planner",
        )
        await app._run_thread_turn_observed(
            {"user": "U", "text": "<@B> Planner: draft", "ts": "1"},
            channel="C",
            web_client=web_client,
            tenant_id=uuid.uuid4(),
            thread_id="1",
            team_id="T",
        )
    assert admit.await_args.kwargs["requested_agent_name"] == "Planner"
    posted = web_client.chat_postMessage.await_args.kwargs
    assert posted["text"] == (
        "This thread is with Daimon.\nStart a new message in the channel to ask Planner."
    )
    assert posted["blocks"][-1]["elements"][0]["value"] == "ag_planner"


async def test_slack_passes_recorded_thread_root_as_authored_candidate() -> None:
    app = object.__new__(SlackApp)
    app.runtime = MagicMock()
    app._bot_user_ids = {"T": "B"}
    web_client = MagicMock(spec=AsyncWebClient)
    web_client.chat_postMessage = AsyncMock()
    authored_id = uuid.uuid4()
    with (
        patch("daimon.adapters.slack.app.resolve_admin_status", new_callable=AsyncMock) as admin,
        patch("daimon.adapters.slack.app.identity_enabled_for", return_value=True),
        patch("daimon.adapters.slack.app.get_post", new_callable=AsyncMock) as get_post,
        patch("daimon.adapters.slack.app.admit", new_callable=AsyncMock) as admit,
    ):
        admin.return_value = True
        get_post.return_value = MagicMock(agent_id=authored_id)
        admit.side_effect = NamedAgentRefused(kind="unavailable")
        await app._run_thread_turn_observed(
            {"user": "U", "text": "<@B> continue", "ts": "2", "thread_ts": "1"},
            channel="C",
            web_client=web_client,
            tenant_id=uuid.uuid4(),
            thread_id="1",
            team_id="T",
        )
    assert admit.await_args.kwargs["authored_agent_id"] == authored_id

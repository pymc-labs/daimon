"""Slack GitHub repo modal and new-repo delivery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack.agent_setup import github_new_repo as notice_module
from daimon.adapters.slack.agent_setup.github_repos import build_view
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.core.github_panel import GrantsPanel, RepoChoice
from daimon.core.stores.github_new_repo_notices import NewRepoNotice


def test_slack_grants_view_shows_staged_and_live_values() -> None:
    panel = GrantsPanel(
        mode="app",
        working_repo="example/work",
        has_pat=False,
        has_pending=True,
        repos=(
            RepoChoice(
                repo_id=1,
                full_name="example/work",
                max_access="write",
                baseline="write",
                ceiling="write",
                staged=False,
                working=True,
                live_baseline="write",
                live_ceiling="write",
            ),
            RepoChoice(
                repo_id=2,
                full_name="example/other",
                max_access="read",
                baseline="read",
                ceiling="read",
                staged=True,
                working=False,
            ),
        ),
    )
    view = build_view(
        PanelMetadata(
            team_id="T",
            channel_id="C",
            view="github_repos",
            agent_name="helper",
        ),
        panel,
    )
    text = "\n".join(
        block["text"]["text"] for block in view["blocks"] if block["type"] == "section"
    )
    actions = [
        element["text"]["text"]
        for block in view["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
        if element["type"] == "button"
    ]
    assert "example/other · staged read baseline / read ceiling · live no grant" in text
    assert "Activate" in actions and "Deactivate" in actions


@pytest.mark.asyncio
async def test_slack_new_repo_card_claim_is_released_on_post_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice = NewRepoNotice(
        tenant_id=uuid.uuid4(),
        installation_id=9,
        repo_full_name="example/new",
        claimed_at=datetime.now(UTC),
    )
    monkeypatch.setattr(notice_module, "claim_next_notice", AsyncMock(return_value=notice))
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(notice_module, "finish_notice", finish)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=None)
    runtime = SimpleNamespace(sessionmaker=SimpleNamespace(begin=lambda: session))
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock(side_effect=RuntimeError("post failed"))
    await notice_module.send_pending_notice(
        runtime, client, team_id="T", channel_id="C", user_id="U"
    )
    assert client.chat_postEphemeral.await_args.kwargs["user"] == "U"
    assert finish.await_args.kwargs["delivered"] is False

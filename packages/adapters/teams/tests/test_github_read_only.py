"""The Teams setup card shows GitHub grants without editing controls."""

from datetime import UTC, datetime

from daimon.adapters.teams.setup_card import details_card
from daimon.core.agent_details import AgentDetails
from daimon.core.github_panel import GrantsPanel, RepoChoice


def test_teams_details_lists_repos_and_working_repo_without_grant_actions() -> None:
    details = AgentDetails(
        ma_agent_id="ag_helper",
        name="Helper",
        model_id="model",
        model_display_name="Model",
        daimon_managed=False,
        created_by_is_workspace=True,
        created_at=datetime.now(UTC),
        answers_here=False,
        applies_note="Changes apply next turn.",
    )
    panel = GrantsPanel(
        mode="app",
        has_pat=False,
        working_repo="owner/work",
        repos=(
            RepoChoice(
                1, "owner/work", "write", "write", "write", False, True, live_ceiling="write"
            ),
            RepoChoice(
                2, "owner/library", "read", "read", "read", False, False, live_ceiling="read"
            ),
        ),
    )
    card = details_card(details, here="channel", page=0, coding_tools=False, github_panel=panel)
    rendered = str(card.model_dump())
    assert "Helper's repos" in rendered
    assert "owner/work" in rendered and "owner/library" in rendered
    assert "Working repo: owner/work" in rendered
    assert "To change repos, ask Helper in chat." in rendered
    assert "Add repos" not in rendered
    assert "Remove from" not in rendered
    assert "Turn off GitHub" not in rendered
    assert "Change access" not in rendered
    empty = details_card(
        details,
        here="channel",
        page=0,
        coding_tools=False,
        github_panel=GrantsPanel(mode="app", has_pat=False, working_repo=None, repos=()),
    )
    assert "No working repo" in str(empty.model_dump())

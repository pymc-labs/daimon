"""Read-only Slack view of an agent's GitHub repositories."""

from __future__ import annotations

from typing import Any, Final

from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.github_panel import GrantsPanel

ACTION_OPEN: Final = "agent_setup__github_repos"
ACTION_BACK: Final = "agent_setup__github_back"
ACTION_CONNECT_LINK: Final = "github_panel__connect_link"


def build_view(
    meta: PanelMetadata, panel: GrantsPanel, *, connect_url: str | None = None
) -> dict[str, Any]:
    name = meta.agent_name or "Agent"
    repos = [repo.full_name for repo in panel.repos if repo.live_ceiling is not None]
    lines = [f"*{escape_mrkdwn(name)}'s repos*"]
    lines.extend(escape_mrkdwn(repo) for repo in repos)
    if not repos:
        lines.append("No repos")
    lines.append(
        f"Working repo: {escape_mrkdwn(panel.working_repo)}"
        if panel.working_repo
        else "No working repo"
    )
    lines.append(f"To change repos, ask {escape_mrkdwn(name)} in chat.")
    actions: list[dict[str, Any]] = []
    if connect_url is not None:
        actions.append(
            {
                "type": "button",
                "action_id": ACTION_CONNECT_LINK,
                "text": {"type": "plain_text", "text": "Connect GitHub"},
                "url": connect_url,
            }
        )
    actions.append(
        {
            "type": "button",
            "action_id": ACTION_BACK,
            "text": {"type": "plain_text", "text": "◀ Back"},
        }
    )
    return finish_modal(
        title="GitHub repos",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
            {"type": "actions", "elements": actions},
        ],
        private_metadata=encode_panel_metadata(meta.with_view("github_repos")),
        callback_id="agent_setup__github_repos",
    )

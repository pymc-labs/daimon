"""Slack modal for per-agent GitHub repo grants."""

from __future__ import annotations

import dataclasses
from typing import Any, Final

from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.github_panel import GrantsPanel

ACTION_OPEN: Final = "agent_setup__github_repos"
ACTION_SELECT: Final = "agent_setup__github_select"
ACTION_STAGE: Final = "agent_setup__github_stage"
ACTION_REMOVE: Final = "agent_setup__github_remove"
ACTION_SWITCH: Final = "agent_setup__github_switch"
ACTION_ACTIVATE: Final = "agent_setup__github_activate"
ACTION_DEACTIVATE: Final = "agent_setup__github_deactivate"
ACTION_PREVIOUS: Final = "agent_setup__github_previous"
ACTION_NEXT: Final = "agent_setup__github_next"


def _button(action: str, label: str, value: str | None = None) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "action_id": action,
        "text": {"type": "plain_text", "text": label},
    }
    if value is not None:
        button["value"] = value
    return button


def build_view(meta: PanelMetadata, panel: GrantsPanel) -> dict[str, Any]:
    """Render the connected repo set and controls for the selected agent."""
    page_count = max(1, (len(panel.repos) + 19) // 20)
    page = min(meta.page, page_count - 1)
    page_repos = panel.repos[page * 20 : (page + 1) * 20]
    selected = next((r for r in page_repos if r.repo_id == meta.repo_id), None)
    if selected is None and page_repos:
        selected = page_repos[0]
    metadata = dataclasses.replace(
        meta, view="github_repos", repo_id=selected.repo_id if selected else None, page=page
    )
    rows: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": escape_mrkdwn(line)}}
        for line in panel.text(meta.agent_name or "Agent", page=page).splitlines()[:30]
    ]
    if panel.repos:
        options = [
            {"text": {"type": "plain_text", "text": r.full_name[:75]}, "value": str(r.repo_id)}
            for r in page_repos
        ]
        selector: dict[str, Any] = {
            "type": "static_select",
            "action_id": ACTION_SELECT,
            "placeholder": {"type": "plain_text", "text": "Choose a repo"},
            "options": options,
        }
        if selected is not None:
            selector["initial_option"] = next(
                item for item in options if item["value"] == str(selected.repo_id)
            )
        rows.append({"type": "actions", "elements": [selector]})
        rows.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_STAGE, "Baseline none", "baseline:none"),
                    _button(ACTION_STAGE, "Baseline read", "baseline:read"),
                    _button(ACTION_STAGE, "Baseline write", "baseline:write"),
                ],
            }
        )
        if page_count > 1:
            nav: list[dict[str, Any]] = []
            if page > 0:
                nav.append(_button(ACTION_PREVIOUS, "Previous repos"))
            if page + 1 < page_count:
                nav.append(_button(ACTION_NEXT, "Next repos"))
            rows.append({"type": "actions", "elements": nav})
        rows.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_STAGE, "Ceiling read", "ceiling:read"),
                    _button(ACTION_STAGE, "Ceiling write", "ceiling:write"),
                    _button(ACTION_REMOVE, "Remove grant"),
                ],
            }
        )
    if panel.mode == "legacy":
        controls: list[dict[str, Any]] = []
        if panel.has_pat:
            controls.append(_button(ACTION_SWITCH, "Switch to GitHub App"))
        controls.append(_button(ACTION_ACTIVATE, "Activate"))
    else:
        controls = []
        if panel.has_pending:
            controls.append(_button(ACTION_ACTIVATE, "Activate"))
        controls.append(_button(ACTION_DEACTIVATE, "Deactivate"))
    rows.append({"type": "actions", "elements": controls})
    return finish_modal(
        title="GitHub repos",
        blocks=rows,
        private_metadata=encode_panel_metadata(metadata),
        callback_id="agent_setup__github_repos",
    )

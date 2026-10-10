"""Slack modal for per-agent GitHub repo grants."""

from __future__ import annotations

import dataclasses
from typing import Any, Final

from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.github_connect_cards import (
    ADD_REPOS_LABEL,
    DETAILS_LABEL,
    AgentRepoLine,
    agent_repos_title,
    agent_section_lines,
    remove_label,
    repo_detail_lines,
)
from daimon.core.github_panel import GrantsPanel
from daimon.core.stores.github_connect import CLIENT_AGENT_MESSAGE

ACTION_OPEN: Final = "agent_setup__github_repos"
ACTION_SELECT: Final = "agent_setup__github_select"
ACTION_STAGE: Final = "agent_setup__github_stage"
ACTION_REMOVE: Final = "agent_setup__github_remove"
ACTION_ACTIVATE: Final = "agent_setup__github_activate"
ACTION_DEACTIVATE: Final = "agent_setup__github_deactivate"
ACTION_PREVIOUS: Final = "agent_setup__github_previous"
ACTION_NEXT: Final = "agent_setup__github_next"
ACTION_CONFIRM_REMOVE: Final = "agent_setup__github_confirm_remove"
ACTION_CONFIRM_TURN_OFF: Final = "agent_setup__github_confirm_turn_off"
ACTION_CANCEL_CONFIRM: Final = "agent_setup__github_cancel_confirm"
ACTION_ADD_OPEN: Final = "agent_setup__github_add_open"
ACTION_SETTINGS: Final = "agent_setup__github_settings"
ACTION_SETTINGS_CHOICE: Final = "agent_setup__github_settings_choice"
ACTION_BACK: Final = "agent_setup__github_back"
ACTION_ADD_FOR: Final = "agent_setup__github_add_for"
"""[Add repos] beside one agent on the GitHub home; the value is the agent name."""


def _button(action: str, label: str, value: str | None = None) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "action_id": action,
        "text": {"type": "plain_text", "text": label},
    }
    if value is not None:
        button["value"] = value
    return button


def build_confirm_view(meta: PanelMetadata, panel: GrantsPanel, *, choice: str) -> dict[str, Any]:
    name = meta.agent_name or "this agent"
    if choice == "remove":
        repo = next((r for r in panel.repos if r.repo_id == meta.repo_id), None)
        if repo is None:
            raise ValueError("Choose a connected repo first.")
        message = (
            f"Remove {repo.full_name} from {name}? It stops using it in new requests. "
            "Chats that already used it keep what was said."
        )
        action = ACTION_CONFIRM_REMOVE
        label = remove_label(name)[:75]
    elif choice == "turn_off":
        message = f"Turn off GitHub for {name}? It loses access to all its repos."
        action = ACTION_CONFIRM_TURN_OFF
        label = "Turn off GitHub"
    else:
        raise ValueError("That GitHub action is unavailable.")
    return finish_modal(
        title="Confirm GitHub change",
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": escape_mrkdwn(message)}},
            {
                "type": "actions",
                "elements": [
                    _button(action, label),
                    _button(ACTION_CANCEL_CONFIRM, "◀ Back"),
                ],
            },
        ],
        private_metadata=encode_panel_metadata(meta.with_view("github_repos", agent_name=name)),
        callback_id="agent_setup__github_confirm",
    )


def live_repo_lines(panel: GrantsPanel) -> tuple[AgentRepoLine, ...]:
    """The repos the agent uses now, as the section lists them."""
    return tuple(
        AgentRepoLine(full_name=repo.full_name, access=repo.live_ceiling)
        for repo in panel.repos
        if repo.live_ceiling is not None
    )


def _text(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def build_view(
    meta: PanelMetadata,
    panel: GrantsPanel,
    *,
    detail_lines: tuple[AgentRepoLine, ...] | None = None,
) -> dict[str, Any]:
    """The agent's repos: the list, who can use them, then Add, Remove and Details."""
    name = meta.agent_name or "Agent"
    live = live_repo_lines(panel)
    page_count = max(1, (len(panel.repos) + 19) // 20)
    page = min(meta.page, page_count - 1)
    page_repos = panel.repos[page * 20 : (page + 1) * 20]
    selected = next((r for r in page_repos if r.repo_id == meta.repo_id), None)
    if selected is None and page_repos:
        selected = page_repos[0]
    metadata = dataclasses.replace(
        meta, view="github_repos", repo_id=selected.repo_id if selected else None, page=page
    )
    if panel.saved_state:
        return finish_modal(
            title="GitHub repos",
            blocks=[
                _text(escape_mrkdwn(CLIENT_AGENT_MESSAGE)),
                {"type": "actions", "elements": [_button(ACTION_BACK, "◀ Back")]},
            ],
            private_metadata=encode_panel_metadata(metadata),
            callback_id="agent_setup__github_repos",
        )
    details = meta.github_settings and meta.github_step == "pick"
    rows: list[dict[str, Any]] = [_text(f"*{escape_mrkdwn(agent_repos_title(name))}*")]
    if details:
        for repo in (detail_lines if detail_lines is not None else live)[:20]:
            rows.append(
                _text(
                    "\n".join(
                        (
                            f"*{escape_mrkdwn(repo.full_name)}*",
                            *map(_keep_mention, repo_detail_lines(repo)),
                        )
                    )
                )
            )
    else:
        body = agent_section_lines(name, live)
        if live:
            rows.append(
                _text(
                    "\n".join(
                        escape_mrkdwn(repo.full_name) for repo in live[page * 20 : (page + 1) * 20]
                    )
                )
            )
            if len(live) > 20:
                rows.append(_text(f"Page {page + 1} of {(len(live) + 19) // 20}"))
        rows.append(_text(escape_mrkdwn(body[-1])))
        if panel.has_pending:
            rows.append(_text("Changes waiting to be saved."))
    if meta.github_settings and meta.github_step.startswith("settings_") and panel.repos:
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
        if page_count > 1:
            nav: list[dict[str, Any]] = []
            if page > 0:
                nav.append(_button(ACTION_PREVIOUS, "Previous repos"))
            if page + 1 < page_count:
                nav.append(_button(ACTION_NEXT, "Next repos"))
            rows.append({"type": "actions", "elements": nav})
        if meta.github_step == "settings_ability":
            rows.append(
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": "*Read only*\nRead code, issues and pull requests.",
                        },
                        {
                            "type": "mrkdwn",
                            "text": (
                                "*Read and write*\nPush branches, open issues and pull requests."
                            ),
                        },
                    ],
                }
            )
            rows.append(
                {
                    "type": "actions",
                    "elements": [
                        _button(ACTION_STAGE, "Read only", "ceiling:read"),
                        _button(ACTION_STAGE, "Read and write", "ceiling:write"),
                    ],
                }
            )
        elif meta.github_step == "settings_remove":
            rows.append(
                {"type": "actions", "elements": [_button(ACTION_REMOVE, remove_label(name)[:75])]}
            )
    controls: list[dict[str, Any]] = []
    if not meta.github_settings:
        controls.append(_button(ACTION_ADD_OPEN, ADD_REPOS_LABEL))
        if live:
            controls.append(_button(ACTION_SETTINGS_CHOICE, remove_label(name)[:75], "remove"))
            controls.append(_button(ACTION_SETTINGS, DETAILS_LABEL))
    if details and live:
        controls.append(_button(ACTION_SETTINGS_CHOICE, "Change access", "ability"))
    if panel.has_pending and meta.github_settings:
        controls.append(_button(ACTION_ACTIVATE, "Save changes"))
    if panel.mode == "app" and details:
        controls.append(_button(ACTION_DEACTIVATE, f"Turn off GitHub for {meta.agent_name}"[:75]))
    controls.append(_button(ACTION_BACK, "◀ Back"))
    rows.append({"type": "divider"})
    rows.append({"type": "actions", "elements": controls})
    return finish_modal(
        title="GitHub repos",
        blocks=rows,
        private_metadata=encode_panel_metadata(metadata),
        callback_id="agent_setup__github_repos",
    )


def _keep_mention(line: str) -> str:
    """Escape a detail line but keep its `<@U…>` mention working."""
    head, mention, tail = line.partition("<@")
    if not mention:
        return escape_mrkdwn(line)
    user, _, rest = tail.partition(">")
    return f"{escape_mrkdwn(head)}<@{user}>{escape_mrkdwn(rest)}"

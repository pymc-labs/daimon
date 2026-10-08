"""Slack modal for choosing repos and adding them to an agent."""

from __future__ import annotations

import dataclasses
from typing import Any, Final

from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.github_panel import GrantsPanel, RepoChoice, suggested_repo
from daimon.core.stores.github_connect import CLIENT_AGENT_MESSAGE

ACTION_OPEN: Final = "agent_setup__github_add_open"
ACTION_SELECT: Final = "agent_setup__github_add_select"
ACTION_PREVIOUS: Final = "agent_setup__github_add_previous"
ACTION_NEXT: Final = "agent_setup__github_add_next"
ACTION_CHANGE: Final = "agent_setup__github_add_change"
ACTION_ABILITY: Final = "agent_setup__github_add_ability"
ACTION_ADD: Final = "agent_setup__github_add_commit"
ACTION_BACK: Final = "agent_setup__github_add_back"
ACTION_CONNECT_MORE: Final = "agent_setup__github_add_connect_more"
ACTION_SUGGEST: Final = "agent_setup__github_add_suggest"

ACTIONS: Final = frozenset(
    {
        ACTION_OPEN,
        ACTION_SELECT,
        ACTION_PREVIOUS,
        ACTION_NEXT,
        ACTION_CHANGE,
        ACTION_ABILITY,
        ACTION_ADD,
        ACTION_BACK,
        ACTION_CONNECT_MORE,
        ACTION_SUGGEST,
    }
)


def _button(action: str, label: str, value: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "button",
        "action_id": action,
        "text": {"type": "plain_text", "text": label},
    }
    if value is not None:
        item["value"] = value
    return item


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": escape_mrkdwn(text)}}


def _selected(meta: PanelMetadata, panel: GrantsPanel) -> tuple[int, ...]:
    del panel
    return meta.selected_repo_ids or ()


def _ability(meta: PanelMetadata, repo: RepoChoice | None = None) -> str:
    read = meta.github_ability == "read" or (repo is not None and repo.max_access == "read")
    return "Read only" if read else "Read and write"


def build_view(
    meta: PanelMetadata, panel: GrantsPanel, *, is_admin: bool = False
) -> dict[str, Any]:
    name = meta.agent_name or "this agent"
    selected = _selected(meta, panel)
    metadata = dataclasses.replace(
        meta,
        view="github_add",
        selected_repo_ids=selected,
    )
    blocks: list[dict[str, Any]] = []
    if panel.saved_state:
        blocks.append(_section(CLIENT_AGENT_MESSAGE))
        blocks.append({"type": "actions", "elements": [_button(ACTION_BACK, "◀ Back")]})
    elif meta.github_step == "pick":
        blocks.extend(
            [
                _section(f"*Repos {name} uses*"),
                {
                    "type": "section",
                    "fields": [{"type": "mrkdwn", "text": f"*Can*\n{_ability(meta)}"}],
                },
                {"type": "divider"},
            ]
        )
        page = min(meta.page, max(0, (len(panel.repos) - 1) // 20))
        page_repos = panel.repos[page * 20 : (page + 1) * 20]
        if page_repos:
            options: list[dict[str, Any]] = [
                {
                    "text": {"type": "plain_text", "text": repo.full_name[:75]},
                    "value": str(repo.repo_id),
                }
                for repo in page_repos
            ]
            select: dict[str, Any] = {
                "type": "multi_static_select",
                "action_id": ACTION_SELECT,
                "placeholder": {"type": "plain_text", "text": "Select repos…"},
                "options": options,
                "initial_options": [
                    option for option in options if int(str(option["value"])) in selected
                ],
                "max_selected_items": len(page_repos),
            }
            blocks.append({"type": "actions", "elements": [select]})
        else:
            blocks.append(_section("No repos connected here."))
        suggestion = suggested_repo(panel.repos, meta.channel_name)
        if suggestion is not None and suggestion.repo_id not in selected:
            blocks.append(_section(f"Suggested: {suggestion.full_name}"))
            blocks.append(
                {
                    "type": "actions",
                    "elements": [_button(ACTION_SUGGEST, "Add", str(suggestion.repo_id))],
                }
            )
        if not is_admin:
            blocks.append(_section("Repo missing? Ask a workspace admin to connect it."))
        if len(panel.repos) > 20:
            nav: list[dict[str, Any]] = []
            if page > 0:
                nav.append(_button(ACTION_PREVIOUS, "Previous repos"))
            if (page + 1) * 20 < len(panel.repos):
                nav.append(_button(ACTION_NEXT, "Next repos"))
            blocks.append({"type": "actions", "elements": nav})
        actions: list[dict[str, Any]] = []
        if panel.repos:
            actions.extend([_button(ACTION_ADD, "Add repos"), _button(ACTION_CHANGE, "Change")])
        if is_admin:
            actions.append(
                _button(
                    ACTION_CONNECT_MORE,
                    "Connect more repos" if panel.repos else "Connect GitHub",
                )
            )
        actions.append(_button(ACTION_BACK, "◀ Back"))
        blocks.append({"type": "actions", "elements": actions})
        metadata = dataclasses.replace(metadata, page=page)
    elif meta.github_step == "change":
        blocks.append(_section("*Can*"))
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": "*Read and write*\nPush branches, open issues and pull requests.",
                    },
                    {"type": "mrkdwn", "text": "*Read only*\nRead code, issues and pull requests."},
                ],
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_ABILITY, "Read and write", "write"),
                    _button(ACTION_ABILITY, "Read only", "read"),
                ],
            }
        )
        blocks.append({"type": "actions", "elements": [_button(ACTION_BACK, "◀ Back")]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "GitHub on Daimon"}]})
    return finish_modal(
        title="Add repos",
        blocks=blocks,
        private_metadata=encode_panel_metadata(metadata),
        callback_id="agent_setup__github_add",
    )

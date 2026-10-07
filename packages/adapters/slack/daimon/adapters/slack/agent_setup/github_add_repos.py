"""Slack modal for choosing and reviewing repos before adding them to an agent."""

from __future__ import annotations

import dataclasses
from typing import Any, Final

from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.github_panel import GrantsPanel, RepoChoice

ACTION_OPEN: Final = "agent_setup__github_add_open"
ACTION_SELECT: Final = "agent_setup__github_add_select"
ACTION_PREVIOUS: Final = "agent_setup__github_add_previous"
ACTION_NEXT: Final = "agent_setup__github_add_next"
ACTION_CHANGE: Final = "agent_setup__github_add_change"
ACTION_ABILITY: Final = "agent_setup__github_add_ability"
ACTION_AUDIENCE: Final = "agent_setup__github_add_audience"
ACTION_REVIEW: Final = "agent_setup__github_add_review"
ACTION_ADD: Final = "agent_setup__github_add_commit"
ACTION_CONFIRM_KEY: Final = "agent_setup__github_add_confirm_key"
ACTION_BACK: Final = "agent_setup__github_add_back"
ACTION_CONNECT_MORE: Final = "agent_setup__github_add_connect_more"

ACTIONS: Final = frozenset(
    {
        ACTION_OPEN,
        ACTION_SELECT,
        ACTION_PREVIOUS,
        ACTION_NEXT,
        ACTION_CHANGE,
        ACTION_ABILITY,
        ACTION_AUDIENCE,
        ACTION_REVIEW,
        ACTION_ADD,
        ACTION_CONFIRM_KEY,
        ACTION_BACK,
        ACTION_CONNECT_MORE,
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
    if meta.selected_repo_ids is not None:
        return meta.selected_repo_ids
    return tuple(
        repo.repo_id
        for repo in panel.repos
        if panel.working_repo is not None
        and repo.full_name.casefold() == panel.working_repo.casefold()
    )


def _audience(meta: PanelMetadata, panel: GrantsPanel) -> str:
    if meta.github_audience is not None:
        return meta.github_audience
    return "github" if any(repo.live_baseline == "none" for repo in panel.repos) else "everyone"


def _ability(meta: PanelMetadata, repo: RepoChoice | None = None) -> str:
    read = meta.github_ability == "read" or (repo is not None and repo.max_access == "read")
    return "read only" if read else "read and open issues and pull requests"


def build_view(
    meta: PanelMetadata, panel: GrantsPanel, *, is_admin: bool = False
) -> dict[str, Any]:
    name = meta.agent_name or "this agent"
    selected = _selected(meta, panel)
    audience = _audience(meta, panel)
    metadata = dataclasses.replace(
        meta,
        view="github_add",
        selected_repo_ids=selected,
        github_audience=audience,
    )
    blocks: list[dict[str, Any]] = []
    if meta.github_step == "pick":
        audience_label = (
            f"everyone who can use {name}"
            if audience == "everyone"
            else "only people with access on GitHub"
        )
        blocks.append(
            _section(
                f"Which repos should {name} use?\nCan: {_ability(meta)} · Used by: {audience_label}"
            )
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
        if not is_admin:
            blocks.append(_section("Repo missing? Ask a workspace admin to connect it."))
        if len(panel.repos) > 20:
            nav: list[dict[str, Any]] = []
            if page > 0:
                nav.append(_button(ACTION_PREVIOUS, "Previous repos"))
            if (page + 1) * 20 < len(panel.repos):
                nav.append(_button(ACTION_NEXT, "Next repos"))
            blocks.append({"type": "actions", "elements": nav})
        actions = [
            _button(ACTION_REVIEW, "Review repos"),
            _button(ACTION_CHANGE, "Change"),
        ]
        if is_admin:
            actions.append(_button(ACTION_CONNECT_MORE, "Connect more repos"))
        actions.append(_button(ACTION_BACK, "◀ Back"))
        blocks.append({"type": "actions", "elements": actions})
        metadata = dataclasses.replace(metadata, page=page)
    elif meta.github_step == "change":
        blocks.append(_section("What can it do? Who can use it?"))
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_ABILITY, "Read and open issues and pull requests", "write"),
                    _button(ACTION_ABILITY, "Read only", "read"),
                ],
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_AUDIENCE, f"Everyone who can use {name}", "everyone"),
                    _button(ACTION_AUDIENCE, "Only people with access on GitHub", "github"),
                    _button(ACTION_BACK, "◀ Back"),
                ],
            }
        )
    elif meta.github_step == "review":
        chosen = [repo for repo in panel.repos if repo.repo_id in selected]
        lines = "\n".join(f"• {repo.full_name} — {_ability(meta, repo)}" for repo in chosen)
        audience_label = (
            f"everyone who can use {name}"
            if audience == "everyone"
            else "only people with access on GitHub"
        )
        blocks.append(_section(f"Add repos to {name}\n{lines}\nUsed by: {audience_label}"))
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_ADD, "Add repos"),
                    _button(ACTION_BACK, "◀ Back"),
                ],
            }
        )
    else:
        blocks.append(
            _section(
                f"Updating {name} deletes its saved GitHub key and restarts its open chats. "
                "Unsaved work in those chats will be lost. Save that work before continuing."
            )
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_CONFIRM_KEY, "Update and restart chats"),
                    _button(ACTION_BACK, "Not now"),
                ],
            }
        )
    return finish_modal(
        title="Add GitHub repos",
        blocks=blocks,
        private_metadata=encode_panel_metadata(metadata),
        callback_id="agent_setup__github_add",
    )

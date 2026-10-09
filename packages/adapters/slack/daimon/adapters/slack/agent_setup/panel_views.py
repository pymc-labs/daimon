"""Pure Block Kit builders for the setup panel.

Three screens — Agents, one agent's Details, and Who answers where — plus the
New agent form and the placeholder shown while a creation is in flight. Every
one of them ends in `finish_modal`, so a view that would break one of Slack's
limits raises here rather than being rejected by the API in front of a person.

The core models carry the facts and the sentences; this module decides only
how a Slack modal says them. It reads no settings, resolves no credential and
re-derives no precedence: an unrouted agent is unrouted because
`AgentDetails.answers_in` is empty, and the sentence about it is the one
`daimon.core.routing_facts` wrote. Admin and member see identical blocks; the
role changes which admin controls are offered, including avatar changes and
the channel admins form.

Pure — no I/O, no clock, no slack_sdk.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    encode_panel_metadata,
)
from daimon.adapters.slack.modal_limits import (
    MAX_SECTION_TEXT_CHARS,
    finish_modal,
    fit_title,
)
from daimon.adapters.slack.mrkdwn import escape_mrkdwn, escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.setup_conversations import setup_button
from daimon.core.agent_detail_lists import DetailListName, format_detail_lists
from daimon.core.agent_details import AgentDetails
from daimon.core.answering_map import AnsweringMap, ChannelAnswer
from daimon.core.channel_admins import MAX_CHANNEL_ADMIN_IDS, fit_lines, fold_mentions
from daimon.core.channel_environments import (
    ENVIRONMENT_OPTION_INHERIT,
    EnvironmentPicker,
    environment_option_value,
)
from daimon.core.channel_rules import READERS_LABELS, WRITERS_LABELS, ChannelRuleStatus
from daimon.core.github_repo_auth import RepoAccess, normalize_owner_repo
from daimon.core.models_catalog import ModelChoice
from daimon.core.panel_operator_tokens import PANEL_SCOPES, PANEL_TTL_DAYS, operator_token_line
from daimon.core.roster import Page, Roster, RosterAgent
from daimon.core.routing_facts import (
    PRECEDENCE_LINE,
    build_routing_request,
)
from daimon.core.scope import AnsweringPlace
from daimon.core.setup_conversations import (
    EMPTY_ROSTER_COPY,
    setup_target_label,
    shared_keys_sentence,
)
from daimon.core.skills.ingest import SkillPreview
from daimon.core.stores.domain import ChannelAdminsRow, ChannelSkillRow, McpTokenRow

__all__ = [
    "ACTION_CHANNEL_ADMINS",
    "ACTION_CHANNEL_SKILLS",
    "ACTION_CODING_TOOLS",
    "ACTION_DETAILS",
    "ACTION_AVATAR_CHANGE",
    "ACTION_AVATAR_DETAILS",
    "ACTION_AVATAR_RESET",
    "CALLBACK_AVATAR_UPLOAD",
    "AVATAR_FILE_INPUT_ID",
    "build_avatar_upload_form",
    "build_avatar_status_view",
    "ACTION_ENVIRONMENT",
    "ACTION_EXPAND_KEYS",
    "ACTION_EXPAND_SKILLS",
    "ACTION_EXPAND_CONNECTIONS",
    "ACTION_NEW",
    "ACTION_OPERATOR_MINT",
    "ACTION_OPERATOR_REVOKE",
    "ACTION_PAGE_NEXT",
    "ACTION_PAGE_PREV",
    "ACTION_REVOKE_TOKEN",
    "ACTION_ROUTING",
    "ACTION_RULE_COPY",
    "ACTION_RULE_READERS",
    "ACTION_RULE_RELEASE",
    "ACTION_RULE_WRITERS",
    "CALLBACK_AGENTS",
    "CALLBACK_CHANNEL_ADMINS",
    "CALLBACK_CHANNEL_SKILLS",
    "CHANNEL_SKILLS_ADD_INPUT_ID",
    "CHANNEL_SKILLS_REMOVE_INPUT_ID",
    "CALLBACK_CREATING",
    "CALLBACK_DETAILS",
    "CALLBACK_NEW_AGENT",
    "CALLBACK_OPERATOR_MINT",
    "CALLBACK_ROUTING",
    "LEGACY_ACTION_IDS",
    "CHANNEL_ADMINS_GROUPS_INPUT_ID",
    "CHANNEL_ADMINS_INPUT_ID",
    "build_agents_view",
    "build_channel_admins_form",
    "build_channel_skills_form",
    "build_created_view",
    "build_creating_view",
    "build_details_view",
    "build_error_view",
    "build_new_agent_form",
    "build_operator_token_form",
    "build_routing_view",
]

# ---------------------------------------------------------------------------
# Action and callback identifiers
# ---------------------------------------------------------------------------

ACTION_DETAILS: Final = "agent_setup__details"
ACTION_AVATAR_CHANGE: Final = "agent_setup__avatar_change"
ACTION_AVATAR_RESET: Final = "agent_setup__avatar_reset"
ACTION_AVATAR_DETAILS: Final = "agent_setup__avatar_details"
CALLBACK_AVATAR_UPLOAD: Final = "agent_setup__avatar_upload"
AVATAR_FILE_INPUT_ID: Final = "agent_setup__avatar_file"
"""Open one agent's Details. The button's `value` is the agent name."""

ACTION_ROUTING: Final = "agent_setup__routing"
ACTION_PAGE_NEXT: Final = "agent_setup__page:next"
ACTION_PAGE_PREV: Final = "agent_setup__page:prev"
ACTION_EXPAND_KEYS: Final = "agent_setup__expand:keys"
ACTION_EXPAND_SKILLS: Final = "agent_setup__expand:skills"
ACTION_EXPAND_CONNECTIONS: Final = "agent_setup__expand:connections"
ACTION_NEW: Final = "agent_setup__new"
ACTION_GITHUB_CONNECT: Final = "agent_setup__github_connect"
ACTION_GITHUB_START: Final = "agent_setup__github_start"
ACTION_GITHUB_CHOOSE_AGENT: Final = "agent_setup__github_choose_agent"
ACTION_GITHUB_BACK: Final = "agent_setup__github_step_back"
ACTION_GITHUB_MANAGE: Final = "agent_setup__github_manage"
ACTION_GITHUB_REPOS: Final = "agent_setup__github_repos"
ACTION_GITHUB_PERSONAL_LINK: Final = "agent_setup__github_personal_link"
ACTION_GITHUB_UNLINK: Final = "agent_setup__github_unlink"
ACTION_GITHUB_UNLINK_CONFIRM: Final = "agent_setup__github_unlink_confirm"
ACTION_GITHUB_UNLINK_BACK: Final = "agent_setup__github_unlink_back"
ACTION_GITHUB_WAITING: Final = "agent_setup__github_waiting_open"


def build_github_home_view(
    meta: PanelMetadata,
    *,
    connected_count: int,
    is_admin: bool = True,
    owners: tuple[str, ...] = (),
    agent_count: int = 0,
    pending_url: str | None = None,
    linked_login: str | None = None,
    can_choose_agent: bool = False,
    own_waiting_count: int = 0,
) -> dict[str, Any]:
    """The private GitHub entry screen for workspace admins."""
    if meta.github_step == "personal_unlink":
        return finish_modal(
            title="GitHub",
            blocks=[
                _section("Unlink GitHub from your account?"),
                {
                    "type": "actions",
                    "elements": [
                        _button(action_id=ACTION_GITHUB_UNLINK_CONFIRM, label="Unlink"),
                        _button(action_id=ACTION_GITHUB_UNLINK_BACK, label="◀ Back"),
                    ],
                },
            ],
            private_metadata=encode_panel_metadata(meta.with_view("github_home")),
            callback_id="agent_setup__github_home",
        )
    if is_admin and pending_url:
        summary = "Connection in progress."
        actions: list[dict[str, Any]] = [
            {
                "type": "button",
                "action_id": "github_link__open",
                "text": {"type": "plain_text", "text": "Continue"},
                "url": pending_url,
            },
            _button(action_id=ACTION_GITHUB_START, label="Start over"),
        ]
    elif not is_admin:
        summary = "Workspace admins connect repos here."
        actions = (
            [_button(action_id=ACTION_GITHUB_CHOOSE_AGENT, label="Choose agent")]
            if can_choose_agent
            else []
        )
    elif connected_count:
        source = f" from {', '.join(owners)}" if owners else ""
        summary = f"Connected: {connected_count} repos{source}."
        actions = [
            _button(action_id=ACTION_GITHUB_CHOOSE_AGENT, label="Choose agent"),
            _button(action_id=ACTION_GITHUB_START, label="Connect more repos"),
            _button(action_id=ACTION_GITHUB_MANAGE, label="Manage connected repos"),
        ]
    else:
        summary = "Connect repos here, then choose which agents can use them."
        actions = [_button(action_id=ACTION_GITHUB_START, label="Connect GitHub")]
    actions.append(_button(action_id=ACTION_GITHUB_BACK, label="◀ Back"))
    status = f"Linked as @{linked_login}" if linked_login else "GitHub isn't linked."
    fields = [{"type": "mrkdwn", "text": f"*Personal link*\n{status}"}]
    if connected_count:
        fields.insert(0, {"type": "mrkdwn", "text": f"*Agents*\n{agent_count} using these repos"})
    blocks: list[dict[str, Any]] = [
        _section(summary),
        {"type": "section", "fields": fields},
        {"type": "divider"},
    ]
    if actions:
        blocks.append({"type": "actions", "elements": actions})
    personal = [
        _button(
            action_id=ACTION_GITHUB_PERSONAL_LINK,
            label="Use another account" if linked_login else "Link GitHub",
        )
    ]
    if linked_login:
        personal.append(_button(action_id=ACTION_GITHUB_UNLINK, label="Unlink"))
    blocks.append({"type": "actions", "elements": personal})
    if is_admin or can_choose_agent or own_waiting_count:
        blocks.append(
            {
                "type": "actions",
                "elements": [_button(action_id=ACTION_GITHUB_WAITING, label="Requests waiting")],
            }
        )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "GitHub on Daimon"}]})
    return finish_modal(
        title="GitHub",
        blocks=blocks,
        private_metadata=encode_panel_metadata(meta.with_view("github_home")),
        callback_id="agent_setup__github_home",
    )


ACTION_CHANNEL_ADMINS: Final = "agent_setup__channel_admins"
ACTION_CHANNEL_SKILLS: Final = "agent_setup__channel_skills"
"""Open the form naming this channel's admins. Workspace admins only."""
ACTION_ENVIRONMENT: Final = "agent_setup__environment"
"""This channel's environment select on Who answers where."""

ACTION_RULE_READERS: Final = "agent_setup__rule:readers"
ACTION_RULE_WRITERS: Final = "agent_setup__rule:writers"
ACTION_RULE_COPY: Final = "agent_setup__rule:copy"
ACTION_RULE_RELEASE: Final = "agent_setup__rule:release"
"""This channel's Permissions: who reads it, who posts, keep it to a copy of its
agent, release its agents. Admins only."""

ACTION_CODING_TOOLS: Final = "agent_setup__coding_tools"
"""Mint a coding-tool token for the agent named in the button's `value`."""

ACTION_ADD_SKILL: Final = "agent_setup__skill_add"
"""Open the Add skill form for the agent on Details."""

ACTION_REVOKE_TOKEN: Final = "agent_setup__revoke_token"
"""Revoke one minted token; the button's `value` is its jti.

Rendered by the coding-tools ephemeral rather than by any view here, but the
identifier belongs with the panel's other action ids so the dispatcher reads
one list.
"""

ACTION_OPERATOR_MINT: Final = "agent_setup__operator_token:mint"
ACTION_OPERATOR_REVOKE: Final = "agent_setup__operator_token:revoke"
"""Open the operator token form, or revoke the token picked in the select. Admins only."""

CALLBACK_AGENTS: Final = "agent_setup"
CALLBACK_DETAILS: Final = "agent_setup__details_view"
CALLBACK_ROUTING: Final = "agent_setup__routing_view"
CALLBACK_NEW_AGENT: Final = "agent_setup__new_agent"
CALLBACK_CREATING: Final = "agent_setup__creating"
CALLBACK_CHANNEL_ADMINS: Final = "agent_setup__channel_admins_form"
CALLBACK_CHANNEL_SKILLS: Final = "agent_setup__channel_skills_form"
CHANNEL_SKILLS_ADD_INPUT_ID: Final = "channel_skills__add"
CHANNEL_SKILLS_REMOVE_INPUT_ID: Final = "channel_skills__remove"
CHANNEL_ADMINS_INPUT_ID: Final = "channel_admins__users"
CHANNEL_ADMINS_GROUPS_INPUT_ID: Final = "channel_admins__groups"
"""Block and action id of the form's member select; the submission reads it."""
CALLBACK_OPERATOR_MINT: Final = "agent_setup__operator_token_form"
OPERATOR_SCOPES_INPUT_ID: Final = "operator_token__scopes"
OPERATOR_LABEL_INPUT_ID: Final = "operator_token__label"
"""Block and action ids of the operator token form's inputs."""
CALLBACK_ADD_SKILL: Final = "agent_setup__add_skill_form"
ADD_SKILL_INPUT_ID: Final = "add_skill__text"
"""Block and action id of the Add skill form's SKILL.md input."""
MAX_SKILL_PASTE_CHARS: Final = 3000
"""Slack's cap on a plain-text input."""

LEGACY_ACTION_IDS: Final[frozenset[str]] = frozenset(
    {
        "agent_setup__roster_select",
        "agent_setup__fork",
        "agent_setup__edit",
        "agent_setup__delete",
        "agent_setup__scope:workspace",
        "agent_setup__scope:channel",
        "agent_setup__scope:clear",
        "agent_setup__tab:agent",
        "agent_setup__tab:repo_auth",
        "agent_setup__tab:skills",
        "agent_setup__tab:mcps",
        "agent_setup__tab:secrets",
        "agent_setup__edit_agent_form",
        "agent_setup__edit_repo_form",
        "agent_setup__add_skill",
        "agent_setup__remove_skill",
        "agent_setup__add_mcp",
        "agent_setup__remove_mcp",
        "agent_setup__connect_mcp",
        "agent_setup__paste_secrets",
        "agent_setup__remove_secret",
    }
)
"""Every action the editor panel emitted that the read-only panel must not.

`agent_setup__new` and `agent_setup__conversation` are deliberately absent:
both survive into the new panel with the same meaning, so a test that walks a
new view's action ids against this set would fail on a reused id that is
correct.
"""

# ---------------------------------------------------------------------------
# Copy and sizing
# ---------------------------------------------------------------------------

MAX_SETUP_LINKS: Final = 10

DETAILS_BUTTON_LABEL: Final = "🔍 Details"
NEW_AGENT_LABEL: Final = "➕ New agent"
ROUTING_LABEL: Final = "📍 Who answers where"
CODING_TOOLS_LABEL: Final = "🧰 Use from your coding tools"
ADD_SKILL_LABEL: Final = "➕ Add skill"
CHANNEL_ADMINS_LABEL: Final = "Channel admins"
MAX_CHANNEL_ADMIN_LINES: Final = 15
MAX_ENVIRONMENT_LINES: Final = 10
MAX_SHOWN_ENVIRONMENT_NAME: Final = 200
"""Characters of an environment name shown before escaping, so the defaults stay bounded."""
_MORE_LINE_RESERVE: Final = len("\n_and 9999 more_")
_MAX_OPTION_TEXT: Final = 75
CHANNEL_ADMINS_LISTING_MAX_CHARS: Final = 2_800
"""Room for the listing, its heading and "and N more" in one section's 3000 characters."""
CHANNEL_ADMINS_NOTE: Final = (
    "Workspace admins run every channel. A channel's admins may change agents that answer "
    "only in channels they run, and pick those channels' default agent. Built-in agents and "
    "the workspace default stay with workspace admins."
)
CHANNEL_SKILLS_LABEL: Final = "Channel skills"
CHANNEL_SKILLS_NOTE: Final = (
    "Added to whatever agent answers in this channel, here only, from the next message: a "
    "workspace library skill, or one uploaded to this channel's agent. Workspace admins only."
)
OPERATOR_TOKENS_LABEL: Final = "Operator tokens"
MAX_OPERATOR_TOKEN_LINES: Final = 25
OPERATOR_TOKENS_NOTE: Final = (
    f"An operator token lets an integration call daimon's tenant tools as you, for "
    f"{PANEL_TTL_DAYS} days. It is shown once; revoke it here or with "
    "`daimon mcp revoke-token`."
)
PERMISSIONS_LABEL: Final = "Permissions"
RULE_COPY_LABEL: Final = "Keep it to a copy"
RULE_RELEASE_LABEL: Final = "Release its agents"
PERMISSIONS_NOTE: Final = (
    "*Who can read it*: anyone; only this channel, so only turns here read its messages "
    "and conversations; or only its own agents, so also only agents kept here run and show "
    "here, and nowhere else. Its default agent becomes one; "
    f"*{RULE_COPY_LABEL}* makes one from the agent answering now. *Who can post*: anyone; "
    "only its own agents; or nobody, daimon included."
)
RULE_RELEASE_NOTE: Final = (
    f"*{RULE_RELEASE_LABEL}* drops the rules keeping its agents here, so they can run "
    "elsewhere, bringing what they remembered here."
)

CODING_TOOLS_UNAVAILABLE_NOTE: Final = "Coding-tool access is not configured for this deployment."

_DETAIL_LIST_MAX_CHARS: Final = 8_000


# ---------------------------------------------------------------------------
# Small block helpers
# ---------------------------------------------------------------------------


def _clip(text: str) -> str:
    """Keep one section's text inside Slack's per-section cap."""
    if len(text) <= MAX_SECTION_TEXT_CHARS:
        return text
    return f"{text[: MAX_SECTION_TEXT_CHARS - 1]}…"


def _section(text: str, *, accessory: dict[str, Any] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": _clip(text)}}
    if accessory is not None:
        block["accessory"] = accessory
    return block


def _context(*lines: str) -> dict[str, Any]:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": _clip(line)} for line in lines],
    }


def _button(
    *, action_id: str, label: str, value: str | None = None, style: str | None = None
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": label, "emoji": True},
    }
    if value is not None:
        element["value"] = value
    if style is not None:
        element["style"] = style
    return element


def _setup_elements(
    target_ma_agent_id: str | None,
    *,
    target_name: str | None,
) -> list[dict[str, Any]]:
    """The Manage button, styled as the view's primary action.

    Taken from `setup_conversations.setup_button` so the action id and `value`
    convention have one home — the panel only restyles it, names the agent it
    carries, and puts it in a row with its neighbours.
    """
    elements: list[dict[str, Any]] = list(setup_button(target_ma_agent_id)["elements"])
    for element in elements:
        element["style"] = "primary"
        element["text"]["text"] = setup_target_label(target_name)
    return elements


def _slack_date(moment: datetime) -> str:
    """A timestamp Slack renders in the reader's own timezone and locale."""
    return f"<!date^{int(moment.timestamp())}^{{date_short}}|{moment.date().isoformat()}>"


def _pager_blocks(page: Page[Any], *, meta: PanelMetadata) -> list[dict[str, Any]]:
    """Page counter and arrows, or nothing at all when there is one page."""
    if page.page_count <= 1:
        return []
    arrows: list[dict[str, Any]] = []
    if page.has_previous:
        arrows.append(
            _button(action_id=ACTION_PAGE_PREV, label="◀ Previous", value=str(page.page - 1))
        )
    if page.has_next:
        arrows.append(_button(action_id=ACTION_PAGE_NEXT, label="Next ▶", value=str(page.page + 1)))
    blocks = [_context(f"Page {page.page + 1} of {page.page_count}")]
    if arrows:
        blocks.append({"type": "actions", "elements": arrows})
    return blocks


def _place_labels(places: Sequence[AnsweringPlace]) -> list[str]:
    labels: list[str] = []
    for place in places:
        if place.tier == "channel" and place.channel_id is not None:
            labels.append(f"<#{place.channel_id}>")
        elif place.tier == "tenant":
            labels.append("the workspace default")
        else:
            labels.append("the deployment default")
    return labels


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def build_agents_view(
    roster: Roster,
    *,
    page: Page[RosterAgent],
    meta: PanelMetadata,
    is_admin: bool,
    attributions: Mapping[uuid.UUID, str],
    channel_id: str,
    routed_agent_names: Collection[str] | None = None,
) -> dict[str, Any]:
    """The root view: who answers here, then everyone else, then the actions.

    `is_admin` is accepted and deliberately unused in the block layout —
    members and admins see the same roster and the same entries, because
    entering a conversation about a change is not the change.

    `routed_agent_names` is every agent this install routes to somewhere. It
    is optional because the roster alone cannot tell "answers in another
    channel" from "answers nowhere", and the panel must not guess: without it
    a row carries no routing claim at all.
    """
    del attributions
    blocks: list[dict[str, Any]] = [_section(f"*Agents in <#{escape_mrkdwn(channel_id)}>*")]
    answering = roster.answering
    if not roster.rows:
        blocks.append(_section(EMPTY_ROSTER_COPY))
    for agent in page.items:
        is_answering = answering is not None and agent.name == answering.name
        status = _roster_status(
            agent,
            is_answering=is_answering,
            channel_id=channel_id,
            routed_agent_names=routed_agent_names,
        )
        heading = f"*{escape_mrkdwn(agent.name)}*\n{status}"
        blocks.append(
            _section(
                heading,
                accessory=_button(
                    action_id=ACTION_DETAILS, label=DETAILS_BUTTON_LABEL, value=agent.name
                ),
            )
        )
    blocks.append({"type": "divider"})
    elements = _setup_elements(
        answering.ma_agent_id if answering is not None else None,
        target_name=answering.name if answering is not None else None,
    )
    elements.append(_button(action_id=ACTION_NEW, label=NEW_AGENT_LABEL))
    elements.append(_button(action_id=ACTION_ROUTING, label=ROUTING_LABEL))
    if is_admin:
        elements.append(_button(action_id=ACTION_GITHUB_CONNECT, label="🐙 GitHub"))
    blocks.append({"type": "actions", "elements": elements})
    blocks.extend(_pager_blocks(page, meta=meta))
    return finish_modal(
        title="Agents",
        blocks=blocks,
        private_metadata=encode_panel_metadata(
            PanelMetadata(
                team_id=meta.team_id,
                channel_id=channel_id,
                view="agents",
                page=page.page,
                root_view_id=meta.root_view_id,
            )
        ),
        callback_id=CALLBACK_AGENTS,
    )


def _roster_status(
    agent: RosterAgent,
    *,
    is_answering: bool,
    channel_id: str,
    routed_agent_names: Collection[str] | None,
) -> str:
    """The one routing fact the roster can prove for an agent."""
    if is_answering:
        return f"Answers in <#{channel_id}>"
    if routed_agent_names is None:
        return "Routing unavailable"
    if agent.name in routed_agent_names:
        return "Answers in another channel"
    return "Not assigned"


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------


def build_details_view(
    details: AgentDetails,
    *,
    meta: PanelMetadata,
    is_admin: bool,
    coding_tools_available: bool,
    channel_id: str,
    attribution: str | None,
    avatar_url: str | None = None,
    avatar_editable: bool | None = None,
) -> dict[str, Any]:
    """One agent's whole readable state, in the order the roster promised.

    `is_admin` controls the avatar buttons. The core phrases
    `details.unrouted_note` in the reader's voice. Key values are not a
    parameter: the model carries names and attribution only.
    """
    del attribution
    title = fit_title(details.name)
    blocks: list[dict[str, Any]] = []
    if title != details.name:
        blocks.append(_section(f"*{escape_mrkdwn(details.name)}*"))
    if details.purpose:
        blocks.append(_section(escape_mrkdwn(details.purpose)))
    blocks.append(_section(_answers_text(details)))
    blocks.append(
        {
            "type": "actions",
            "elements": _setup_elements(
                details.ma_agent_id,
                target_name=details.name,
            ),
        }
    )
    blocks.append(_section(f"*Model:* {escape_mrkdwn(details.model_display_name)}"))
    show_avatar = not details.daimon_managed if avatar_editable is None else avatar_editable
    avatar_accessory = (
        {"type": "image", "image_url": avatar_url, "alt_text": f"{details.name}'s picture"}
        if show_avatar and avatar_url
        else None
    )
    if show_avatar:
        blocks.append(
            _section("*Picture*\nShown next to this agent's messages.", accessory=avatar_accessory)
        )
    if is_admin and show_avatar:
        reset_button = _button(
            action_id=ACTION_AVATAR_RESET, label="Use default", value=details.name
        )
        reset_button["confirm"] = {
            "title": {"type": "plain_text", "text": "Use default picture?"},
            "text": {
                "type": "mrkdwn",
                "text": "Use the default picture?",
            },
            "confirm": {"type": "plain_text", "text": "Use default"},
            "deny": {"type": "plain_text", "text": "Cancel"},
        }
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(action_id=ACTION_AVATAR_CHANGE, label="Change", value=details.name),
                    reset_button,
                    _button(action_id=ACTION_AVATAR_DETAILS, label="Details", value=details.name),
                ],
            }
        )
    blocks.extend(_repo_blocks(details))
    blocks.extend(_detail_list_blocks(details, meta=meta))
    actions = [
        _button(action_id=ACTION_GITHUB_REPOS, label="🐙 GitHub repos", value=details.name),
        _button(action_id=ACTION_ADD_SKILL, label=ADD_SKILL_LABEL, value=details.name),
    ]
    if coding_tools_available:
        actions.insert(
            0,
            _button(action_id=ACTION_CODING_TOOLS, label=CODING_TOOLS_LABEL, value=details.name),
        )
    blocks.append({"type": "actions", "elements": actions})
    if not coding_tools_available:
        blocks.append(_context(CODING_TOOLS_UNAVAILABLE_NOTE))
    return finish_modal(
        title=title,
        blocks=blocks,
        private_metadata=encode_panel_metadata(
            PanelMetadata(
                team_id=meta.team_id,
                channel_id=channel_id,
                view="details",
                agent_name=details.name,
                root_view_id=meta.root_view_id,
                expanded=meta.expanded,
            )
        ),
        callback_id=CALLBACK_DETAILS,
    )


def build_avatar_upload_form(*, meta: PanelMetadata) -> dict[str, Any]:
    """A single image upload for the agent in the details view."""
    return finish_modal(
        title="Change picture",
        blocks=[
            _section("Choose a picture. Up to 2 MB."),
            {
                "type": "input",
                "block_id": AVATAR_FILE_INPUT_ID,
                "label": {"type": "plain_text", "text": "Picture"},
                "element": {
                    "type": "file_input",
                    "action_id": AVATAR_FILE_INPUT_ID,
                    "filetypes": ["png", "jpg", "jpeg", "gif", "webp"],
                    "max_files": 1,
                },
            },
            {"type": "divider"},
            _context("File types: PNG, JPG, GIF or WebP."),
        ],
        private_metadata=encode_panel_metadata(meta),
        callback_id=CALLBACK_AVATAR_UPLOAD,
        close="Cancel",
        submit="Save",
    )


def build_avatar_details_view(*, meta: PanelMetadata) -> dict[str, Any]:
    """Keep public-image and cache guidance behind Details."""
    return finish_modal(
        title="Picture details",
        blocks=[
            _section("Anyone who sees a message can open the picture."),
            _section("The old picture may still appear for a while."),
            {"type": "divider"},
            _context("Agent setup"),
        ],
        private_metadata=encode_panel_metadata(meta),
        callback_id="agent_setup__avatar_details_view",
        close="Done",
    )


def build_avatar_status_view(
    *, meta: PanelMetadata, message: str, external_id: str | None = None, retry: bool = False
) -> dict[str, Any]:
    """Show upload progress or a result in the submitted modal itself."""
    title, separator, next_step = message.partition(". ")
    view = finish_modal(
        title="Picture",
        blocks=[
            _section(title + ("." if separator else "")),
            *([_section(next_step)] if next_step else []),
            {"type": "divider"},
            *(
                [
                    {
                        "type": "actions",
                        "elements": [
                            _button(
                                action_id=ACTION_AVATAR_CHANGE,
                                label="Choose picture",
                                value=meta.agent_name or "",
                            )
                        ],
                    },
                ]
                if retry
                else []
            ),
            _context("Agent setup"),
        ],
        private_metadata=encode_panel_metadata(meta),
        callback_id=CALLBACK_AVATAR_UPLOAD,
    )
    if external_id is not None:
        view["external_id"] = external_id
    return view


def _answers_text(details: AgentDetails) -> str:
    labels = _place_labels(details.answers_in)
    if len(labels) == 1:
        return f"*Answers in:* {labels[0]}"
    if labels:
        listing = "\n".join(labels)
        return f"*Answers in*\n{listing}"
    return escape_mrkdwn_preserving_mentions(details.unrouted_note or "Not assigned yet.")


def _link_label(text: str) -> str:
    """Keep an external-service name inside one Slack link label."""
    return escape_mrkdwn(text).replace("|", "¦").replace("\r", " ").replace("\n", " ")


def _link_url(url: str) -> str:
    """Encode characters that could terminate or split Slack's link markup."""
    return escape_mrkdwn(url.replace("|", "%7C").replace("\r", "%0D").replace("\n", "%0A"))


def _detail_list_blocks(details: AgentDetails, *, meta: PanelMetadata) -> list[dict[str, Any]]:
    items: Mapping[DetailListName, Sequence[str]] = {
        "skills": [escape_mrkdwn(skill.title or skill.skill_id) for skill in details.skills],
        "connections": [
            f"<{_link_url(server.url)}|{_link_label(server.name)}>"
            for server in details.mcp_servers
        ],
        "keys": [escape_mrkdwn(key.name) for key in details.keys],
    }
    formatted = format_detail_lists(
        items,
        expanded=meta.expanded,
        max_chars=_DETAIL_LIST_MAX_CHARS,
        max_list_chars=MAX_SECTION_TEXT_CHARS - 32,
    )
    labels: tuple[tuple[DetailListName, str, str], ...] = (
        ("skills", "Skills", ACTION_EXPAND_SKILLS),
        ("connections", "Connections", ACTION_EXPAND_CONNECTIONS),
        ("keys", "Keys", ACTION_EXPAND_KEYS),
    )
    blocks: list[dict[str, Any]] = []
    for kind, heading, action_id in labels:
        values = items[kind]
        if not values:
            if kind == "skills" and details.skills_listing_truncated:
                blocks.append(_context("Some skill names may be missing."))
            continue
        accessory = (
            _button(
                action_id=action_id,
                label="Show fewer" if meta.expanded == kind else "Show more",
            )
            if len(values) > 6
            else None
        )
        blocks.append(_section(f"*{heading}*\n{formatted[kind]}", accessory=accessory))
        if kind == "skills" and details.skills_listing_truncated:
            blocks.append(_context("Some skill names may be missing."))
        if kind == "keys":
            blocks.append(_context(shared_keys_sentence(escape_mrkdwn(details.name))))
    return blocks


def _repo_blocks(details: AgentDetails) -> list[dict[str, Any]]:
    repo = details.repo
    if repo is None:
        return []
    owner_repo = normalize_owner_repo(repo.repo_url)
    link = f"<https://github.com/{owner_repo}|{escape_mrkdwn(owner_repo)}>"
    blocks = [_section(f"*Repository:* {link}")]
    if repo.default_branch:
        blocks.append(_section(f"*Branch:* `{escape_mrkdwn(repo.default_branch)}`"))
    access_lines = _repo_access_lines(repo.access)
    if access_lines:
        blocks.append(_context(*access_lines))
    return blocks


def _repo_access_lines(access: RepoAccess) -> list[str]:
    """Say exactly what was recorded about this repo, and nothing more."""
    if access.kind == "needs_attention":
        corrective = access.corrective or "nothing would authorize a clone right now."
        return [f"⚠️ needs attention: {escape_mrkdwn(corrective)}"]
    if access.kind == "not_checked":
        return ["Not checked yet"]
    if access.credential == "per_agent_token":
        lead = "via token"
    elif access.credential == "deployment_public":
        lead = "public repo"
    else:
        lead = "via the GitHub App"
    if access.checked_at is None:
        return [lead]
    return [lead, f"Last checked {_slack_date(access.checked_at)}"]


# ---------------------------------------------------------------------------
# Who answers where
# ---------------------------------------------------------------------------


def build_routing_view(
    answering_map: AnsweringMap,
    *,
    page: Page[ChannelAnswer],
    meta: PanelMetadata,
    is_admin: bool,
    attributions: Mapping[uuid.UUID, str],
    setup_links: Sequence[str],
    channel_id: str,
    unrouted_agent_name: str | None,
    channel_admins: Sequence[ChannelAdminsRow] | None = None,
    environment_picker: EnvironmentPicker | None = None,
    rule_status: ChannelRuleStatus | None = None,
    operator_tokens: Sequence[McpTokenRow] | None = None,
    channel_skills: Sequence[ChannelSkillRow] | None = None,
) -> dict[str, Any]:
    """The whole cascade, laid out so the precedence is visible, not inferred.

    No setup button: this view answers where mentions go, and the change it
    describes is a sentence to say to Daimon rather than a control here.
    `channel_admins` is passed for workspace admins only, who also see every
    channel's admins and a button to edit this channel's. `rule_status` is too,
    when the panel has a channel, not a DM: it adds that channel's permissions.
    `operator_tokens` is too: the workspace's live operator tokens, with Mint and Revoke.
    `channel_skills` is too, with a channel: its extra skills, with Edit.
    The environment each channel runs in resolves on its own and gets its own
    block; `environment_picker` is passed for workspace admins and this
    channel's admins, who get a select for this channel's environment.
    """
    blocks: list[dict[str, Any]] = []
    if page.items:
        for answer in page.items:
            blocks.append(
                _section(
                    f"*Channel:* <#{answer.channel_id}>\n"
                    f"*Agent:* {escape_mrkdwn(answer.agent_name)}"
                )
            )
            audit = _audit_parts(
                account_id=answer.set_by_account_id,
                moment=answer.set_at,
                attributions=attributions,
            )
            if audit:
                blocks.append(_context(*audit))
    else:
        blocks.append(_section("_no channel has its own setting_"))
    tenant_default = answering_map.tenant_default
    if tenant_default is not None:
        blocks.append(_section(f"*Workspace default:* {escape_mrkdwn(tenant_default.agent_name)}"))
        audit = _audit_parts(
            account_id=tenant_default.set_by_account_id,
            moment=tenant_default.set_at,
            attributions=attributions,
        )
        if audit:
            blocks.append(_context(*audit))
    else:
        blocks.append(_section("*Workspace default:* Not assigned"))
    if answering_map.deployment_default is not None:
        line = f"Deployment default: *{escape_mrkdwn(answering_map.deployment_default)}*"
        if answering_map.tenant_consumes_fallthrough:
            line = f"{line}\n_not in effect while a workspace default is set_"
        blocks.append(_section(line))
    else:
        blocks.append(_section("_no deployment default_"))
    blocks.append({"type": "divider"})
    blocks.extend(_environment_blocks(answering_map, picker=environment_picker))
    links = list(setup_links[:MAX_SETUP_LINKS])
    listing = "\n".join(links) if links else "_none open_"
    blocks.append(_section(f"*Setup conversations*\n{listing}"))
    blocks.append({"type": "divider"})
    if channel_admins is not None:
        blocks.extend(_channel_admins_blocks(channel_admins, channel_id=channel_id))
    if rule_status is not None and _is_channel(channel_id):
        blocks.extend(_permissions_blocks(channel_id=channel_id, status=rule_status))
    if channel_skills is not None and _is_channel(channel_id):
        blocks.extend(_channel_skills_blocks(channel_skills, channel_id=channel_id))
    if operator_tokens is not None:
        blocks.extend(_operator_token_blocks(operator_tokens))
    blocks.append(
        _context(
            _routing_request_line(
                answering_map,
                channel_id=channel_id,
                is_admin=is_admin,
                unrouted_agent_name=unrouted_agent_name,
            )
        )
    )
    blocks.extend(_pager_blocks(page, meta=meta))
    return finish_modal(
        title="Who answers where",
        blocks=blocks,
        private_metadata=encode_panel_metadata(
            PanelMetadata(
                team_id=meta.team_id,
                channel_id=channel_id,
                view="routing",
                page=page.page,
                agent_name=unrouted_agent_name,
                root_view_id=meta.root_view_id,
            )
        ),
        callback_id=CALLBACK_ROUTING,
    )


def _environment_blocks(
    answering_map: AnsweringMap, *, picker: EnvironmentPicker | None
) -> list[dict[str, Any]]:
    heading = "*Environments*"
    tenant, deployment = answering_map.tenant_environment, answering_map.deployment_environment
    defaults = [f"*Workspace default:* {_shown_name(tenant) if tenant else 'Not assigned'}"]
    if deployment is not None:
        defaults.append(f"Deployment default: *{_shown_name(deployment)}*")
        if tenant is not None:
            defaults.append("_not in effect while a workspace default is set_")
    rows = answering_map.channel_environments
    lines = fit_lines(
        (
            f"<#{row.channel_id}> → *{_shown_name(row.environment_name)}*"
            for row in rows[:MAX_ENVIRONMENT_LINES]
        ),
        max_chars=MAX_SECTION_TEXT_CHARS
        - len("\n".join([heading, *defaults]))
        - 1
        - _MORE_LINE_RESERVE,
    )
    if len(lines) < len(rows):
        lines.append(f"_and {len(rows) - len(lines)} more_")
    if not rows:
        lines.append("_no channel picks its own environment yet_")
    blocks = [_section("\n".join([heading, *lines, *defaults]))]
    if picker is not None:
        blocks.append({"type": "actions", "elements": [_environment_select(picker)]})
    blocks.append({"type": "divider"})
    return blocks


def _shown_name(name: str) -> str:
    return escape_mrkdwn(name[:MAX_SHOWN_ENVIRONMENT_NAME])


def _environment_select(picker: EnvironmentPicker) -> dict[str, Any]:
    """This channel's environment select, its current choice pre-selected."""

    def option(label: str, value: str) -> dict[str, Any]:
        return {"text": {"type": "plain_text", "text": label[:_MAX_OPTION_TEXT]}, "value": value}

    inherited = f" ({picker.inherited})" if picker.inherited else ""
    options = [option(f"Use the default{inherited}", ENVIRONMENT_OPTION_INHERIT)]
    options += [option(name, environment_option_value(name)) for name in picker.names]
    current = (
        ENVIRONMENT_OPTION_INHERIT if picker.own is None else environment_option_value(picker.own)
    )
    element: dict[str, Any] = {
        "type": "static_select",
        "action_id": ACTION_ENVIRONMENT,
        "placeholder": {"type": "plain_text", "text": "Environment for this channel"},
        "options": options,
    }
    initial = next((opt for opt in options if opt["value"] == current), None)
    if initial is not None:
        element["initial_option"] = initial
    return element


def _is_channel(channel_id: str) -> bool:
    """A DM (`D…`) has no channel admins and takes no channel rule."""
    return bool(channel_id) and not channel_id.startswith("D")


def _admin_mentions(row: ChannelAdminsRow) -> list[str]:
    """User groups first, then members, as Discord lists roles before members."""
    return [f"<!subteam^{group_id}>" for group_id in row.role_ids] + [
        f"<@{uid}>" for uid in row.user_ids
    ]


def _channel_admins_blocks(
    grants: Sequence[ChannelAdminsRow], *, channel_id: str
) -> list[dict[str, Any]]:
    lines = fit_lines(
        (
            f"<#{row.channel_id}> → {fold_mentions(_admin_mentions(row))}"
            for row in grants[:MAX_CHANNEL_ADMIN_LINES]
        ),
        max_chars=CHANNEL_ADMINS_LISTING_MAX_CHARS,
    )
    if len(lines) < len(grants):
        lines.append(f"_and {len(grants) - len(lines)} more_")
    listing = "\n".join(lines) or "_no channel has its own admins yet_"
    editable = _is_channel(channel_id)
    edit = _button(action_id=ACTION_CHANNEL_ADMINS, label="Edit this channel") if editable else None
    return [
        _section(f"*{CHANNEL_ADMINS_LABEL}*\n{listing}", accessory=edit),
        _context(CHANNEL_ADMINS_NOTE),
        {"type": "divider"},
    ]


def _channel_skills_blocks(
    rows: Sequence[ChannelSkillRow], *, channel_id: str
) -> list[dict[str, Any]]:
    names = ", ".join(f"`{escape_mrkdwn(row.name)}`" for row in rows) or "_none_"
    edit = _button(action_id=ACTION_CHANNEL_SKILLS, label="Edit")
    return [
        _section(f"*{CHANNEL_SKILLS_LABEL}* of <#{channel_id}>\n{names}", accessory=edit),
        _context(CHANNEL_SKILLS_NOTE),
        {"type": "divider"},
    ]


def _rule_select[T: str](
    action_id: str, labels: Mapping[T, str], current: T, text: str
) -> dict[str, Any]:
    options = [
        {"text": {"type": "plain_text", "text": f"{text}: {label}"}, "value": value}
        for value, label in labels.items()
    ]
    return {
        "type": "static_select",
        "action_id": action_id,
        "placeholder": {"type": "plain_text", "text": text},
        "options": options,
        "initial_option": next(opt for opt in options if opt["value"] == current),
    }


def _permissions_blocks(*, channel_id: str, status: ChannelRuleStatus) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = [
        _rule_select(ACTION_RULE_READERS, READERS_LABELS, status.rule.readers, "Who can read it"),
        _rule_select(ACTION_RULE_WRITERS, WRITERS_LABELS, status.rule.writers, "Who can post"),
    ]
    notes = [PERMISSIONS_NOTE]
    if status.rule.readers != "own":
        elements.append(_button(action_id=ACTION_RULE_COPY, label=RULE_COPY_LABEL))
        if status.agents:
            elements.append(
                _button(action_id=ACTION_RULE_RELEASE, label=RULE_RELEASE_LABEL, style="danger")
            )
            notes.append(RULE_RELEASE_NOTE)
    return [
        _section(f"*{PERMISSIONS_LABEL}* of <#{channel_id}>\n{escape_mrkdwn(status.line)}"),
        {"type": "actions", "elements": elements},
        _context(" ".join(notes)),
        {"type": "divider"},
    ]


def _operator_token_blocks(rows: Sequence[McpTokenRow]) -> list[dict[str, Any]]:
    shown = rows[:MAX_OPERATOR_TOKEN_LINES]
    listing = "\n".join(f"`{operator_token_line(row)}`" for row in shown)
    blocks = [
        _section(
            f"*{OPERATOR_TOKENS_LABEL}*\n{listing or '_no live operator tokens_'}",
            accessory=_button(action_id=ACTION_OPERATOR_MINT, label="Mint a token"),
        )
    ]
    if shown:
        options = [
            {
                "text": {"type": "plain_text", "text": operator_token_line(row)[:_MAX_OPTION_TEXT]},
                "value": str(row.jti),
            }
            for row in shown
        ]
        revoke = {
            "type": "static_select",
            "action_id": ACTION_OPERATOR_REVOKE,
            "placeholder": {"type": "plain_text", "text": "Revoke a token"},
            "options": options,
        }
        blocks.append({"type": "actions", "elements": [revoke]})
    blocks += [_context(OPERATOR_TOKENS_NOTE), {"type": "divider"}]
    return blocks


def build_operator_token_form(*, meta: PanelMetadata) -> dict[str, Any]:
    """Pick the tenant scopes and an optional label for a new operator token."""
    options = [
        {"text": {"type": "plain_text", "text": scope}, "value": scope} for scope in PANEL_SCOPES
    ]
    return finish_modal(
        title="Mint an operator token",
        blocks=[
            {
                "type": "input",
                "block_id": OPERATOR_SCOPES_INPUT_ID,
                "label": {"type": "plain_text", "text": "Scopes"},
                "element": {
                    "type": "checkboxes",
                    "action_id": OPERATOR_SCOPES_INPUT_ID,
                    "options": options,
                },
            },
            {
                "type": "input",
                "block_id": OPERATOR_LABEL_INPUT_ID,
                "label": {"type": "plain_text", "text": "Label"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": OPERATOR_LABEL_INPUT_ID,
                    "max_length": 100,
                },
            },
            _context(OPERATOR_TOKENS_NOTE),
        ],
        private_metadata=encode_panel_metadata(meta),
        callback_id=CALLBACK_OPERATOR_MINT,
        close="Cancel",
        submit="Mint",
    )


GROUP_GRANT_WARNING: Final = (
    "Anyone who can join or edit a user group picked here becomes this channel's admin. "
    "Slack lets every member edit user groups by default, so limit user group management "
    "to admins in the workspace settings first."
)
GROUPS_UNLISTED_NOTE: Final = (
    "User groups couldn't be listed (the app needs the usergroups:read scope), so saving "
    "keeps this channel's groups as they are."
)


def build_channel_admins_form(
    *,
    meta: PanelMetadata,
    user_ids: Sequence[str],
    group_ids: Sequence[str] = (),
    groups: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Name who runs `meta.channel_id` besides the workspace admins; empty clears it.

    Members, and user groups whose members all count. `groups` labels the
    workspace's user groups; None when they couldn't be listed, which leaves
    the groups out of the form and the stored ones untouched on save.
    """
    element: dict[str, Any] = {
        "type": "multi_users_select",
        "action_id": CHANNEL_ADMINS_INPUT_ID,
        "max_selected_items": MAX_CHANNEL_ADMIN_IDS,
        "placeholder": {"type": "plain_text", "text": "Pick members"},
    }
    if user_ids:
        element["initial_users"] = list(user_ids)
    blocks: list[dict[str, Any]] = [
        _section(f"Who runs <#{meta.channel_id}> besides the workspace admins."),
        {
            "type": "input",
            "block_id": CHANNEL_ADMINS_INPUT_ID,
            "label": {"type": "plain_text", "text": "Members"},
            "optional": True,
            "element": element,
        },
    ]
    if groups is None:
        blocks.append(_context(GROUPS_UNLISTED_NOTE))
    elif group_input := _groups_input(groups, group_ids):
        blocks += [group_input, _context(GROUP_GRANT_WARNING)]
    blocks.append(_context(f"Empty the lists to clear it. {CHANNEL_ADMINS_NOTE}"))
    return finish_modal(
        title=CHANNEL_ADMINS_LABEL,
        blocks=blocks,
        private_metadata=encode_panel_metadata(meta),
        callback_id=CALLBACK_CHANNEL_ADMINS,
        close="Cancel",
        submit="Save",
    )


def build_channel_skills_form(
    *, meta: PanelMetadata, rows: Sequence[ChannelSkillRow]
) -> dict[str, Any]:
    """Add a skill to `meta.channel_id`, or tick its current ones to remove them."""
    blocks: list[dict[str, Any]] = [
        _section(f"Extra skills for whatever agent answers in <#{meta.channel_id}>."),
        {
            "type": "input",
            "block_id": CHANNEL_SKILLS_ADD_INPUT_ID,
            "label": {"type": "plain_text", "text": "Add a skill"},
            "optional": True,
            "element": {
                "type": "plain_text_input",
                "action_id": CHANNEL_SKILLS_ADD_INPUT_ID,
                "max_length": 200,
                "placeholder": {"type": "plain_text", "text": "name, agent/name or skill id"},
            },
        },
    ]
    if rows:
        blocks.append(
            {
                "type": "input",
                "block_id": CHANNEL_SKILLS_REMOVE_INPUT_ID,
                "label": {"type": "plain_text", "text": "Remove"},
                "optional": True,
                "element": {
                    "type": "checkboxes",
                    "action_id": CHANNEL_SKILLS_REMOVE_INPUT_ID,
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": row.name[:_MAX_OPTION_TEXT]},
                            "value": row.skill_id,
                        }
                        for row in rows[:10]
                    ],
                },
            }
        )
    blocks.append(_context(CHANNEL_SKILLS_NOTE))
    return finish_modal(
        title=CHANNEL_SKILLS_LABEL,
        blocks=blocks,
        private_metadata=encode_panel_metadata(meta),
        callback_id=CALLBACK_CHANNEL_SKILLS,
        close="Cancel",
        submit="Save",
    )


def _groups_input(groups: Mapping[str, str], group_ids: Sequence[str]) -> dict[str, Any] | None:
    """A user group multi-select, or None when the workspace has none to offer.

    A stored group Slack no longer lists stays an option, so saving the
    form doesn't drop it unseen. A static select holds 100 options.
    """
    labels = {gid: groups.get(gid, f"unknown group {gid}") for gid in group_ids}
    labels |= {gid: label for gid, label in groups.items() if gid not in labels}
    if not labels:
        return None
    options = [
        {
            "text": {"type": "plain_text", "text": label[:_MAX_OPTION_TEXT]},
            "value": gid,
        }
        for gid, label in list(labels.items())[:100]
    ]
    element: dict[str, Any] = {
        "type": "multi_static_select",
        "action_id": CHANNEL_ADMINS_GROUPS_INPUT_ID,
        "max_selected_items": MAX_CHANNEL_ADMIN_IDS,
        "placeholder": {"type": "plain_text", "text": "Pick user groups"},
        "options": options,
    }
    initial = [option for option in options if option["value"] in group_ids]
    if initial:
        element["initial_options"] = initial
    return {
        "type": "input",
        "block_id": CHANNEL_ADMINS_GROUPS_INPUT_ID,
        "label": {"type": "plain_text", "text": "User groups"},
        "optional": True,
        "element": element,
    }


def _file_list(paths: Sequence[str], *, shown: int = 15) -> str:
    listed = [f"`{escape_mrkdwn(path[:80])}`" for path in paths[:shown]]
    if len(paths) > shown:
        listed.append(f"+{len(paths) - shown} more")
    return ", ".join(listed)


def build_add_skill_form(
    *,
    meta: PanelMetadata,
    text: str = "",
    preview: SkillPreview | None = None,
    files_in_chat: bool = True,
) -> dict[str, Any]:
    """Paste a SKILL.md; with `preview`, show what it holds and submit to add it.

    Slack modals take no files, so a .zip goes through chat instead, which
    only works where chat has a confirmation card (`files_in_chat`). The
    previewed hash travels in `meta.skill_hash`; submitting changed text
    previews again rather than adding. The description gets its own section,
    so a long one is clipped without hiding the files.
    """
    agent = escape_mrkdwn(meta.agent_name or "this agent")
    blocks: list[dict[str, Any]] = []
    if preview is None:
        blocks.append(_section("Paste a SKILL.md. You see what it holds before anything is added."))
    else:
        blocks.append(
            _section(
                f"*Add {escape_mrkdwn(preview.name)} to {agent}?*\n"
                f"{escape_mrkdwn(preview.description)}"
            )
        )
        blocks.append(_section(f"*Files:* {_file_list(preview.files)}"))
        if preview.scripts:
            blocks.append(_section(f"⚠️ *{agent} could run:* {_file_list(preview.scripts)}"))
    element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": ADD_SKILL_INPUT_ID,
        "multiline": True,
        "max_length": MAX_SKILL_PASTE_CHARS,
    }
    if text:
        element["initial_value"] = text
    blocks.append(
        {
            "type": "input",
            "block_id": ADD_SKILL_INPUT_ID,
            "label": {"type": "plain_text", "text": "SKILL.md"},
            "element": element,
        }
    )
    note = (
        f"Submit to add it as {agent}'s own skill; shared skills are not changed. "
        "Changed text is previewed again."
        if preview is not None
        else f"For a .zip or a file, attach it in a message and ask me to add it to {agent}."
        if files_in_chat
        else "Slack forms take no files, so paste the SKILL.md here."
    )
    blocks.append(_context(note))
    return finish_modal(
        title="Add skill",
        blocks=blocks,
        private_metadata=encode_panel_metadata(meta),
        callback_id=CALLBACK_ADD_SKILL,
        close="Cancel",
        submit="Add" if preview is not None else "Preview",
    )


def _audit_parts(
    *,
    account_id: uuid.UUID | None,
    moment: datetime | None,
    attributions: Mapping[uuid.UUID, str],
) -> list[str]:
    parts: list[str] = []
    mention = attributions.get(account_id) if account_id is not None else None
    if mention is not None:
        parts.append(f"set by {mention}")
    if moment is not None:
        parts.append(_slack_date(moment))
    return parts


def _answering_name(answering_map: AnsweringMap, *, channel_id: str) -> str | None:
    """Which agent this channel's mentions reach today, by the same precedence."""
    for answer in answering_map.channel_overrides:
        if answer.channel_id == channel_id:
            return answer.agent_name
    if answering_map.tenant_default is not None:
        return answering_map.tenant_default.agent_name
    return answering_map.deployment_default


def _routing_request_line(
    answering_map: AnsweringMap,
    *,
    channel_id: str,
    is_admin: bool,
    unrouted_agent_name: str | None,
) -> str:
    agent_name = unrouted_agent_name or _answering_name(answering_map, channel_id=channel_id)
    lead = "Tell Daimon: " if is_admin else "An admin can tell Daimon: "
    request = build_routing_request(
        agent_name=escape_mrkdwn(agent_name or "an agent"), channel_label=f"<#{channel_id}>"
    )
    return f"{PRECEDENCE_LINE} {lead}{request}"


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


def build_error_view(*, request_id: str) -> dict[str, Any]:
    """Error modal shown when background content fetch fails (Loading-modal pattern).

    Replaces the "Loading…" placeholder via ``views.update`` so the modal is
    never left in a permanent spinner state.

    Args:
        request_id: Opaque request identifier for support cross-referencing.

    Returns:
        A modal view dict safe to pass to ``views.update(view=...)``.
    """
    text = f":x: *Couldn’t load agent setup.* Please try again. (ref: {escape_mrkdwn(request_id)})"
    return {
        "type": "modal",
        "callback_id": CALLBACK_AGENTS,
        "title": {"type": "plain_text", "text": "Agent Setup"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        ],
    }


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def build_creating_view(*, agent_name: str, meta: PanelMetadata) -> dict[str, Any]:
    """The placeholder the New agent form acks into while the create runs."""
    return finish_modal(
        title="New agent",
        blocks=[
            _section(f"Creating *{escape_mrkdwn(agent_name)}*…"),
            _context("This takes a few seconds."),
        ],
        private_metadata=encode_panel_metadata(
            meta.with_view("creating", agent_name=agent_name, root_view_id=meta.root_view_id)
        ),
        callback_id=CALLBACK_CREATING,
    )


def build_created_view(
    *, agent_name: str, meta: PanelMetadata, isolated_here: bool
) -> dict[str, Any]:
    """What the placeholder becomes when the new agent can't be shown to its creator here."""
    name = escape_mrkdwn(agent_name)
    text = (
        f"*{name}* was created. This channel is kept to its own agents, so it shows here once it "
        "is set as the channel's agent."
        if isolated_here
        else f"*{name}* was created but is not listed yet. Reopen setup to see it."
    )
    return finish_modal(
        title="New agent",
        blocks=[_section(text)],
        private_metadata=encode_panel_metadata(
            meta.with_view("creating", agent_name=agent_name, root_view_id=meta.root_view_id)
        ),
        callback_id=CALLBACK_CREATING,
    )


def build_new_agent_form(
    *,
    meta: PanelMetadata,
    model_choices: Sequence[ModelChoice],
    initial_name: str | None = None,
    initial_purpose: str | None = None,
    initial_model: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The one creation shortcut: name, purpose, model, and nothing else.

    The block and action ids are the ones the submission evaluator already
    reads, so a failed create can be restored into this same form with the
    person's inputs intact rather than handing them a blank one.
    """
    options = [
        {
            "text": {"type": "plain_text", "text": choice.label},
            "value": choice.id,
        }
        for choice in model_choices
    ]
    selected_id = initial_model or next(
        (choice.id for choice in model_choices if choice.is_default),
        model_choices[0].id if model_choices else None,
    )
    initial_option = next((option for option in options if option["value"] == selected_id), None)
    model_element: dict[str, Any] = {
        "type": "static_select",
        "action_id": "new_agent__model",
        "options": options,
    }
    if initial_option is not None:
        model_element["initial_option"] = initial_option
    name_element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": "new_agent__name",
        "placeholder": {"type": "plain_text", "text": "e.g. my-data-analyst"},
    }
    if initial_name:
        name_element["initial_value"] = initial_name
    purpose_element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": "new_agent__prompt",
        "multiline": True,
        "placeholder": {"type": "plain_text", "text": "One or two sentences…"},
    }
    if initial_purpose:
        purpose_element["initial_value"] = initial_purpose
    blocks: list[dict[str, Any]] = []
    if error is not None:
        blocks.append(_section(f"⚠️ {escape_mrkdwn(error)}"))
    blocks.extend(
        [
            {
                "type": "input",
                "block_id": "new_agent__name",
                "label": {"type": "plain_text", "text": "Agent name"},
                "element": name_element,
            },
            {
                "type": "input",
                "block_id": "new_agent__prompt",
                "label": {"type": "plain_text", "text": "What should it help with?"},
                "optional": True,
                "element": purpose_element,
            },
            {
                "type": "input",
                "block_id": "new_agent__model",
                "label": {"type": "plain_text", "text": "Model"},
                "element": model_element,
            },
        ]
    )
    return finish_modal(
        title="New agent",
        blocks=blocks,
        private_metadata=encode_panel_metadata(
            meta.with_view("new_agent", root_view_id=meta.root_view_id)
        ),
        callback_id=CALLBACK_NEW_AGENT,
        close="Cancel",
        submit="Create",
    )

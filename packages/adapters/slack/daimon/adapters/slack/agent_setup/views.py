"""Pure Block Kit view builders for the /agent-setup modal stack (SUX-01, Phase 83).

All functions return plain ``dict[str, Any]`` objects — no ``slack_sdk.models``
types (project convention). This module imports only stdlib + ``state.py`` +
``daimon.adapters.slack.mrkdwn`` (no slack_sdk, no daimon.core). No I/O.

Structural Guarantees enforced here:
  #1 (D-09): ``build_secrets_section`` accepts ``secret_names: list[str]`` ONLY.
             Secret VALUES never appear as a parameter, in a block, block_id, or action_id.
  #4 (D-08): PAT/MCP token display is ``****{last4}`` — builders receive the
             pre-masked string; raw tokens are never a parameter.

Block Kit limits:
  - Modal title ≤ 24 chars  (Pitfall 6)
  - private_metadata ≤ 3,000 chars  (Pitfall 6)
  - static_select ≤ 25 options  (Pitfall 6)
"""

from __future__ import annotations

from typing import Any

from daimon.adapters.slack.agent_setup.state import (
    AgentSetupState,
    encode_private_metadata,
)
from daimon.adapters.slack.mrkdwn import escape_mrkdwn

__all__ = [
    "build_loading_view",
    "build_error_view",
    "build_l1_view",
    "build_l2_view",
    "build_agent_section",
    "build_repo_auth_section",
    "build_skills_section",
    "build_mcps_section",
    "build_secrets_section",
    "build_l3_new_agent_form",
    "build_l3_fork_agent_form",
    "build_l3_edit_agent_form",
    "build_l3_edit_repo_form",
    "build_l3_add_skill_form",
    "build_l3_add_mcp_form",
    "build_l3_paste_secrets_form",
]

_ROSTER_CAP = 25
_SECRET_CAP = 20

# ---------------------------------------------------------------------------
# Loading / error views
# ---------------------------------------------------------------------------


def build_loading_view(*, team_id: str, channel_id: str) -> dict[str, Any]:
    """Lightweight loading modal opened immediately with trigger_id (D-06).

    Opened via ``views.open``; caller captures ``resp["view"]["id"]`` then
    ``views.update`` with the real content once loaded.

    Args:
        team_id:    Slack workspace ID from slash-command payload.
        channel_id: Invoking channel from slash-command payload.

    Returns:
        A modal view dict safe to pass to ``views.open(view=...)``.
    """
    return {
        "type": "modal",
        "callback_id": "agent_setup",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            selected_agent_name=None,
        ),
        "title": {"type": "plain_text", "text": "Agent Setup"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Loading…"},
            }
        ],
    }


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
        "callback_id": "agent_setup",
        "title": {"type": "plain_text", "text": "Agent Setup"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        ],
    }


# ---------------------------------------------------------------------------
# L1 — Entry modal (Roster + Actions + Scope Picker)
# ---------------------------------------------------------------------------


def build_l1_view(
    state: AgentSetupState,
    *,
    is_admin: bool,
    team_id: str,
    channel_id: str,
    selected_agent_name: str | None,
    scope_hint: str,
) -> dict[str, Any]:
    """Build the L1 entry modal: roster static_select + lifecycle + scope picker.

    Admin rendering includes lifecycle (New/Fork/Edit/Delete) and scope
    (workspace / channel / clear) blocks. Non-admin rendering omits both.

    Zero-agents state, >25 overflow, and no-agent-selected state are all
    handled per the UI-SPEC block inventory.

    Args:
        state:                 Panel state carrying the capped roster rows.
        is_admin:              Whether the invoking user has admin permissions.
        team_id:               Slack workspace ID.
        channel_id:            Invoking channel (default for scope channel picker).
        selected_agent_name:   Currently-selected agent name, or None.
        scope_hint:            Current effective scope description (mrkdwn string).

    Returns:
        A modal view dict for ``views.open`` or ``views.update``.
    """
    blocks: list[dict[str, Any]] = []

    # Block 1: header
    blocks.append(
        {
            "type": "section",
            "block_id": "agent_setup__header",
            "text": {"type": "mrkdwn", "text": ":robot_face: *Agent Setup*"},
        }
    )

    # Block 2: divider
    blocks.append({"type": "divider"})

    # Zero-agents state
    if not state.rows:
        if is_admin:
            empty_text = (
                "_No agents yet. Use *New* to create your first agent, or_ `@bot help me set up`_._"
            )
        else:
            empty_text = "_No agents have been set up for this workspace yet._"
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": empty_text},
            }
        )
    else:
        # Block 3: roster static_select
        options: list[dict[str, Any]] = [
            {
                "text": {"type": "plain_text", "text": row.agent_name},
                "value": row.agent_name,
            }
            for row in state.rows[:_ROSTER_CAP]
        ]

        initial_option: dict[str, Any] | None = None
        if selected_agent_name:
            for row in state.rows[:_ROSTER_CAP]:
                if row.agent_name == selected_agent_name:
                    initial_option = {
                        "text": {
                            "type": "plain_text",
                            "text": row.agent_name,
                        },
                        "value": row.agent_name,
                    }
                    break

        roster_element: dict[str, Any] = {
            "type": "static_select",
            "action_id": "agent_setup__roster_select",
            "placeholder": {"type": "plain_text", "text": "Select agent"},
            "options": options,
        }
        if initial_option is not None:
            roster_element["initial_option"] = initial_option

        blocks.append(
            {
                "type": "input",
                "block_id": "agent_setup__roster_select",
                "label": {"type": "plain_text", "text": "Select agent"},
                "element": roster_element,
                "dispatch_action": True,
            }
        )

        # Overflow context hint (>25 agents)
        if state.over_cap_count > 0:
            total = len(state.rows) + state.over_cap_count
            overflow_text = f"_Showing 25 of {total} agents. Use_ `@bot list agents` _to see all._"
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": overflow_text}],
                }
            )

        # Block 4: lifecycle actions (admin-only)
        if is_admin:
            blocks.append(
                {
                    "type": "actions",
                    "block_id": "agent_setup__lifecycle_actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "agent_setup__new",
                            "text": {"type": "plain_text", "text": "New"},
                        },
                        {
                            "type": "button",
                            "action_id": "agent_setup__fork",
                            "text": {"type": "plain_text", "text": "Fork"},
                        },
                        {
                            "type": "button",
                            "action_id": "agent_setup__edit",
                            "text": {"type": "plain_text", "text": "Edit"},
                        },
                        {
                            "type": "button",
                            "action_id": "agent_setup__delete",
                            "text": {"type": "plain_text", "text": "Delete"},
                            "style": "danger",
                        },
                    ],
                }
            )

        # Block 5: divider before scope section
        blocks.append({"type": "divider"})

        if selected_agent_name is None:
            # No-agent-selected state: replace scope blocks 6-8 with a context
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                "_Select an agent above to view or set its propagation scope._"
                            ),
                        }
                    ],
                }
            )
        else:
            # Block 6: scope header
            blocks.append(
                {
                    "type": "section",
                    "block_id": "agent_setup__scope_header",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":link: *Propagation scope*",
                    },
                }
            )

            # Block 7: scope actions (admin-only)
            if is_admin:
                blocks.append(
                    {
                        "type": "actions",
                        "block_id": "agent_setup__scope_actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "agent_setup__scope:workspace",
                                "text": {
                                    "type": "plain_text",
                                    "text": "[Whole workspace]",
                                },
                            },
                            {
                                "type": "channels_select",
                                "action_id": "agent_setup__scope:channel",
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "[This channel]",
                                },
                                "initial_channel": channel_id,
                            },
                            {
                                "type": "button",
                                "action_id": "agent_setup__scope:clear",
                                "text": {"type": "plain_text", "text": "Clear"},
                            },
                        ],
                    }
                )

            # Block 8: scope hint (always visible)
            blocks.append(
                {
                    "type": "context",
                    "block_id": "agent_setup__scope_hint",
                    "elements": [{"type": "mrkdwn", "text": scope_hint}],
                }
            )

    return {
        "type": "modal",
        "callback_id": "agent_setup",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            selected_agent_name=selected_agent_name,
        ),
        "title": {"type": "plain_text", "text": "Agent Setup"},
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# L2 — Section editor
# ---------------------------------------------------------------------------

_SECTION_TABS: list[tuple[str, str]] = [
    ("Agent", "agent_setup__tab:agent"),
    ("Repo+Auth", "agent_setup__tab:repo_auth"),
    ("Skills", "agent_setup__tab:skills"),
    ("MCPs", "agent_setup__tab:mcps"),
    ("Secrets", "agent_setup__tab:secrets"),
]

_ACTIVE_SECTION_TO_ACTION_ID: dict[str, str] = {
    "agent": "agent_setup__tab:agent",
    "repo_auth": "agent_setup__tab:repo_auth",
    "skills": "agent_setup__tab:skills",
    "mcps": "agent_setup__tab:mcps",
    "secrets": "agent_setup__tab:secrets",
}


def build_l2_view(
    *,
    agent_name: str,
    active_section: str,
    team_id: str,
    channel_id: str,
    is_admin: bool,
    section_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the L2 section editor: header + tabs + section content.

    Section tab switches use ``views.update`` (not ``views.push``) to swap L2
    in place - preserving the level-3 push budget for input forms (D-01, D-02).

    The active tab gets ``"style": "primary"`` to orient the user after each swap.

    Args:
        agent_name:      Name of the agent being edited.
        active_section:  One of: agent, repo_auth, skills, mcps, secrets.
        team_id:         Slack workspace ID.
        channel_id:      Invoking channel (for ephemeral deliveries).
        is_admin:        Whether the invoking user has admin permissions.
        section_blocks:  Pre-built blocks for the active section's content.

    Returns:
        A modal view dict for ``views.push`` (initial) or ``views.update`` (tab swap).
    """
    active_action_id = _ACTIVE_SECTION_TO_ACTION_ID.get(active_section, "")

    tab_elements: list[dict[str, Any]] = []
    for label, action_id in _SECTION_TABS:
        element: dict[str, Any] = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": label},
        }
        if action_id == active_action_id:
            element["style"] = "primary"
        tab_elements.append(element)

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "block_id": "agent_setup__edit_header",
            "text": {
                "type": "mrkdwn",
                "text": f":pencil2: *Editing: {escape_mrkdwn(agent_name)}*",
            },
        },
        {
            "type": "actions",
            "block_id": "agent_setup__section_tabs",
            "elements": tab_elements,
        },
        {"type": "divider"},
        *section_blocks,
    ]

    return {
        "type": "modal",
        "callback_id": "agent_setup__section",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            agent_name=agent_name,
            active_section=active_section,
        ),
        "title": {"type": "plain_text", "text": "Edit agent"},
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# L2 per-section block builders
# ---------------------------------------------------------------------------


def build_agent_section(
    *,
    agent_name: str,
    model_id: str,
    system_prompt: str,
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Build the Agent section blocks for the L2 editor.

    Args:
        agent_name:    Name of the agent (read-only; rename = Fork + Delete).
        model_id:      MA model identifier.
        system_prompt: Full system prompt text (preview truncated to 200 chars).
        is_admin:      Whether mutation actions should be included.

    Returns:
        List of Block Kit blocks for the Agent section.
    """
    prompt_preview = system_prompt[:200]
    if len(system_prompt) > 200:
        prompt_preview += "…"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "block_id": "agent_setup__agent_name",
            "text": {
                "type": "mrkdwn",
                "text": (f"*Name:* `{escape_mrkdwn(agent_name)}` _(rename via Fork + Delete)_"),
            },
        },
        {
            "type": "section",
            "block_id": "agent_setup__agent_model",
            "text": {
                "type": "mrkdwn",
                "text": f"*Model:* `{escape_mrkdwn(model_id)}`",
            },
        },
        {
            "type": "section",
            "block_id": "agent_setup__agent_prompt_preview",
            "text": {
                "type": "mrkdwn",
                "text": escape_mrkdwn(prompt_preview) if prompt_preview else "_(none)_",
            },
        },
    ]

    if is_admin:
        blocks.append(
            {
                "type": "actions",
                "block_id": "agent_setup__agent_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "agent_setup__edit_agent_form",
                        "text": {
                            "type": "plain_text",
                            "text": "Edit prompt & model",
                        },
                    }
                ],
            }
        )

    return blocks


def build_repo_auth_section(
    *,
    repo: str | None,
    pat_last4: str | None,
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Build the Repo+Auth section blocks for the L2 editor.

    Args:
        repo:      Owner/repo string, or None if not configured.
        pat_last4: Pre-masked PAT display string (e.g. ``****abcd``), or None.
        is_admin:  Whether mutation actions should be included.

    Returns:
        List of Block Kit blocks for the Repo+Auth section.
    """
    repo_text = (
        f":file_folder: *Repo:* `{escape_mrkdwn(repo)}`"
        if repo
        else ":file_folder: *Repo:* _(none)_"
    )
    pat_text = (
        f":key: *PAT:* `{escape_mrkdwn(pat_last4)}`" if pat_last4 else ":key: *PAT:* _(none)_"
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "block_id": "agent_setup__repo_binding",
            "text": {"type": "mrkdwn", "text": repo_text},
        },
        {
            "type": "section",
            "block_id": "agent_setup__repo_auth",
            "text": {"type": "mrkdwn", "text": pat_text},
        },
    ]

    if is_admin:
        blocks.append(
            {
                "type": "actions",
                "block_id": "agent_setup__repo_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "agent_setup__edit_repo_form",
                        "text": {"type": "plain_text", "text": "Edit repo + auth"},
                    },
                ],
            }
        )

    return blocks


def build_skills_section(
    *,
    skill_names: list[str],
    sync_pending: bool,
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Build the Skills section blocks for the L2 editor.

    Args:
        skill_names:  List of skill names currently attached to the agent.
        sync_pending: Whether a skill-sync is in progress.
        is_admin:     Whether mutation actions should be included.

    Returns:
        List of Block Kit blocks for the Skills section.
    """
    if skill_names:
        skills_text = "\n".join(f"· `{escape_mrkdwn(name)}`" for name in skill_names)
    else:
        skills_text = "_(none)_"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "block_id": "agent_setup__skills_list",
            "text": {"type": "mrkdwn", "text": skills_text},
        },
    ]

    if sync_pending:
        blocks.append(
            {
                "type": "context",
                "block_id": "agent_setup__skills_sync_hint",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":hourglass_flowing_sand: _Skill sync in progress…_",
                    }
                ],
            }
        )

    if is_admin:
        blocks.append(
            {
                "type": "actions",
                "block_id": "agent_setup__skills_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "agent_setup__add_skill",
                        "text": {"type": "plain_text", "text": "Add skill"},
                    },
                    {
                        "type": "static_select",
                        "action_id": "agent_setup__remove_skill",
                        "placeholder": {"type": "plain_text", "text": "Remove skill…"},
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": name,
                                },
                                "value": name,
                            }
                            for name in skill_names
                        ]
                        if skill_names
                        else [
                            {
                                "text": {"type": "plain_text", "text": "(none)"},
                                "value": "__none__",
                            }
                        ],
                    },
                ],
            }
        )

    return blocks


def build_mcps_section(
    *,
    mcps: list[dict[str, str]],
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Build the MCPs section blocks for the L2 editor.

    Args:
        mcps:     List of MCP server dicts with keys ``name`` and ``url``.
        is_admin: Whether mutation actions should be included.

    Returns:
        List of Block Kit blocks for the MCPs section.
    """
    if mcps:
        mcps_text = "\n".join(
            f"· `{escape_mrkdwn(m['name'])}` — {escape_mrkdwn(m.get('url', ''))}" for m in mcps
        )
    else:
        mcps_text = "_(none)_"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "block_id": "agent_setup__mcps_list",
            "text": {"type": "mrkdwn", "text": mcps_text},
        },
    ]

    if is_admin:
        blocks.append(
            {
                "type": "actions",
                "block_id": "agent_setup__mcps_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "agent_setup__add_mcp",
                        "text": {"type": "plain_text", "text": "Add MCP server"},
                    },
                    {
                        "type": "static_select",
                        "action_id": "agent_setup__remove_mcp",
                        "placeholder": {"type": "plain_text", "text": "Remove MCP…"},
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": m["name"],
                                },
                                "value": m["name"],
                            }
                            for m in mcps
                        ]
                        if mcps
                        else [
                            {
                                "text": {"type": "plain_text", "text": "(none)"},
                                "value": "__none__",
                            }
                        ],
                    },
                    {
                        "type": "button",
                        "action_id": "agent_setup__connect_mcp",
                        "text": {"type": "plain_text", "text": "Connect via MCP"},
                    },
                ],
            }
        )

    return blocks


def build_secrets_section(
    *,
    agent_name: str,
    secret_names: list[str],
    is_admin: bool,
) -> list[dict[str, Any]]:
    """Build the Secrets section blocks for the L2 editor.

    STRUCTURAL GUARANTEE (D-09, Threat T-83-03): this function accepts
    ``secret_names: list[str]`` ONLY. Secret VALUES are never a parameter,
    never appear in any block, block_id, or action_id. Key names render as
    `` `KEY` · `KEY2` `` chips; empty state renders a collapse hint.

    Args:
        agent_name:   Name of the agent (used only for context, not rendered).
        secret_names: List of secret KEY NAMES (values never passed here).
        is_admin:     Whether mutation actions should be included.

    Returns:
        List of Block Kit blocks for the Secrets section.
    """
    if secret_names:
        keys_text = " · ".join(f"`{escape_mrkdwn(name)}`" for name in secret_names)
    else:
        keys_text = "_-# + add your first secret_"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "block_id": "agent_setup__secrets_keys",
            "text": {"type": "mrkdwn", "text": keys_text},
        },
        {
            "type": "context",
            "block_id": "agent_setup__secrets_hint",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_values are write-only; only key names are shown_",
                }
            ],
        },
    ]

    if is_admin:
        blocks.append(
            {
                "type": "actions",
                "block_id": "agent_setup__secrets_actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "agent_setup__paste_secrets",
                        "text": {"type": "plain_text", "text": "Add secrets"},
                    },
                    {
                        "type": "static_select",
                        "action_id": "agent_setup__remove_secret",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Remove secret…",
                        },
                        "options": [
                            {
                                "text": {
                                    "type": "plain_text",
                                    "text": name,
                                },
                                "value": name,
                            }
                            for name in secret_names
                        ]
                        if secret_names
                        else [
                            {
                                "text": {"type": "plain_text", "text": "(none)"},
                                "value": "__none__",
                            }
                        ],
                    },
                ],
            }
        )

    return blocks


# ---------------------------------------------------------------------------
# L3 input forms (push, submit, pop)
# ---------------------------------------------------------------------------


def build_l3_new_agent_form(
    *,
    team_id: str,
    channel_id: str,
    agent_name: str = "",
    parent_section: str | None = None,
) -> dict[str, Any]:
    """Build the New Agent input form (L3 push).

    Args:
        team_id:        Slack workspace ID.
        channel_id:     Invoking channel.
        agent_name:     Unused; reserved for consistency. Pass empty string.
        parent_section: Which L2 section to update after pop (typically None -> L1).

    Returns:
        A modal view dict for ``views.push``.
    """
    return {
        "type": "modal",
        "callback_id": "agent_setup__new_agent",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            agent_name=agent_name or "",
            parent_section=parent_section,
        ),
        "title": {"type": "plain_text", "text": "New agent"},
        "submit": {"type": "plain_text", "text": "Create"},
        "blocks": [
            {
                "type": "input",
                "block_id": "new_agent__name",
                "label": {"type": "plain_text", "text": "Agent name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "new_agent__name",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. my-data-analyst",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "new_agent__prompt",
                "label": {"type": "plain_text", "text": "System prompt"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "new_agent__prompt",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "You are a helpful assistant…",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "new_agent__model",
                "label": {"type": "plain_text", "text": "Model ID"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "new_agent__model",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Leave blank for default",
                    },
                },
            },
        ],
    }


def build_l3_fork_agent_form(
    *,
    source_name: str,
    team_id: str,
    channel_id: str,
    parent_section: str | None = None,
) -> dict[str, Any]:
    """Build the Fork Agent input form (L3 push).

    Args:
        source_name:    Name of the agent being forked (shown read-only).
        team_id:        Slack workspace ID.
        channel_id:     Invoking channel.
        parent_section: Which L2 section to update after pop (typically None -> L1).

    Returns:
        A modal view dict for ``views.push``.
    """
    return {
        "type": "modal",
        "callback_id": "agent_setup__fork_agent",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            agent_name=source_name,
            parent_section=parent_section,
        ),
        "title": {"type": "plain_text", "text": "Fork agent"},
        "submit": {"type": "plain_text", "text": "Fork"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Forking from: `{escape_mrkdwn(source_name)}`",
                },
            },
            {
                "type": "input",
                "block_id": "fork_agent__name",
                "label": {"type": "plain_text", "text": "New agent name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "fork_agent__name",
                    "placeholder": {
                        "type": "plain_text",
                        "text": f"{source_name}-fork",
                    },
                },
            },
        ],
    }


def build_l3_edit_agent_form(
    *,
    agent_name: str,
    model_id: str,
    system_prompt: str,
    team_id: str,
    channel_id: str,
    parent_section: str | None = "agent",
) -> dict[str, Any]:
    """Build the Edit Agent prompt & model form (L3 push).

    System prompt is pre-filled only if <= 3000 chars (Slack input block limit).
    Longer prompts are omitted (blank field + user re-enters).

    Args:
        agent_name:     Name of the agent (shown read-only; rename is forbidden).
        model_id:       Current model identifier (pre-filled).
        system_prompt:  Current system prompt (pre-filled if <= 3000 chars).
        team_id:        Slack workspace ID.
        channel_id:     Invoking channel.
        parent_section: Which L2 section to update after pop (default: ``agent``).

    Returns:
        A modal view dict for ``views.push``.
    """
    prompt_element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": "edit_agent__prompt",
        "multiline": True,
    }
    # Omit pre-fill if prompt exceeds Slack's 3000-char input block limit.
    if len(system_prompt) <= 3000:
        prompt_element["initial_value"] = system_prompt

    return {
        "type": "modal",
        "callback_id": "agent_setup__edit_agent",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            agent_name=agent_name,
            parent_section=parent_section,
        ),
        "title": {"type": "plain_text", "text": "Edit agent"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Name:* `{escape_mrkdwn(agent_name)}`",
                },
            },
            {
                "type": "input",
                "block_id": "edit_agent__prompt",
                "label": {"type": "plain_text", "text": "System prompt"},
                "optional": True,
                "element": prompt_element,
            },
            {
                "type": "input",
                "block_id": "edit_agent__model",
                "label": {"type": "plain_text", "text": "Model ID"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edit_agent__model",
                    "initial_value": model_id,
                },
            },
        ],
    }


def build_l3_edit_repo_form(
    *,
    team_id: str,
    channel_id: str,
    agent_name: str,
    parent_section: str | None = "repo_auth",
) -> dict[str, Any]:
    """Build the Edit Repo+Auth input form (L3 push).

    PAT field is always empty (write-only; blank = keep existing).

    Args:
        team_id:        Slack workspace ID.
        channel_id:     Invoking channel.
        agent_name:     Name of the agent being configured.
        parent_section: Which L2 section to update after pop (default: ``repo_auth``).

    Returns:
        A modal view dict for ``views.push``.
    """
    return {
        "type": "modal",
        "callback_id": "agent_setup__edit_repo",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            agent_name=agent_name,
            parent_section=parent_section,
        ),
        "title": {"type": "plain_text", "text": "Repo + Auth"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {
                "type": "input",
                "block_id": "edit_repo__url",
                "label": {
                    "type": "plain_text",
                    "text": "GitHub repo URL or owner/repo",
                },
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edit_repo__url",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "owner/repo or https://github.com/owner/repo",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "edit_repo__pat",
                "label": {
                    "type": "plain_text",
                    "text": "Personal Access Token",
                },
                "optional": True,
                "hint": {
                    "type": "plain_text",
                    "text": "Leave blank to keep existing token",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edit_repo__pat",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Leave blank to keep existing token",
                    },
                },
            },
        ],
    }


def build_l3_add_skill_form(
    *,
    team_id: str,
    channel_id: str,
    agent_name: str,
    parent_section: str | None = "skills",
) -> dict[str, Any]:
    """Build the Add Skill input form (L3 push).

    Args:
        team_id:        Slack workspace ID.
        channel_id:     Invoking channel.
        agent_name:     Name of the agent receiving the skill.
        parent_section: Which L2 section to update after pop (default: ``skills``).

    Returns:
        A modal view dict for ``views.push``.
    """
    return {
        "type": "modal",
        "callback_id": "agent_setup__add_skill",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            agent_name=agent_name,
            parent_section=parent_section,
        ),
        "title": {"type": "plain_text", "text": "Add skill"},
        "submit": {"type": "plain_text", "text": "Add"},
        "blocks": [
            {
                "type": "input",
                "block_id": "add_skill__repo_url",
                "label": {"type": "plain_text", "text": "Skill repo URL"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "add_skill__repo_url",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "https://github.com/owner/skill-repo",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "add_skill__branch",
                "label": {"type": "plain_text", "text": "Branch"},
                "optional": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "add_skill__branch",
                    "placeholder": {"type": "plain_text", "text": "main"},
                },
            },
        ],
    }


def build_l3_add_mcp_form(
    *,
    team_id: str,
    channel_id: str,
    agent_name: str,
    parent_section: str | None = "mcps",
) -> dict[str, Any]:
    """Build the Add MCP Server input form (L3 push).

    Auth token field is write-only (empty by default).

    Args:
        team_id:        Slack workspace ID.
        channel_id:     Invoking channel.
        agent_name:     Name of the agent receiving the MCP server.
        parent_section: Which L2 section to update after pop (default: ``mcps``).

    Returns:
        A modal view dict for ``views.push``.
    """
    return {
        "type": "modal",
        "callback_id": "agent_setup__add_mcp",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            agent_name=agent_name,
            parent_section=parent_section,
        ),
        "title": {"type": "plain_text", "text": "Add MCP server"},
        "submit": {"type": "plain_text", "text": "Add"},
        "blocks": [
            {
                "type": "input",
                "block_id": "add_mcp__name",
                "label": {"type": "plain_text", "text": "Server name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "add_mcp__name",
                    "placeholder": {"type": "plain_text", "text": "e.g. my-mcp-server"},
                },
            },
            {
                "type": "input",
                "block_id": "add_mcp__url",
                "label": {"type": "plain_text", "text": "Endpoint URL"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "add_mcp__url",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "https://my-mcp-server.example.com",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "add_mcp__token",
                "label": {"type": "plain_text", "text": "Auth token"},
                "optional": True,
                "hint": {
                    "type": "plain_text",
                    "text": "Leave blank to keep existing token",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "add_mcp__token",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Leave blank to keep existing token",
                    },
                },
            },
        ],
    }


def build_l3_paste_secrets_form(
    *,
    team_id: str,
    channel_id: str,
    agent_name: str,
    parent_section: str | None = "secrets",
) -> dict[str, Any]:
    """Build the Paste Secrets input form (L3 push).

    Accepts KEY=VALUE lines (multiline). Values never appear in any block or
    block_id — they only exist in the user's textarea input and are parsed on
    submit by the view_submission handler.

    Args:
        team_id:        Slack workspace ID.
        channel_id:     Invoking channel.
        agent_name:     Name of the agent receiving the secrets.
        parent_section: Which L2 section to update after pop (default: ``secrets``).

    Returns:
        A modal view dict for ``views.push``.
    """
    return {
        "type": "modal",
        "callback_id": "agent_setup__paste_secrets",
        "private_metadata": encode_private_metadata(
            team_id=team_id,
            channel_id=channel_id,
            agent_name=agent_name,
            parent_section=parent_section,
        ),
        "title": {"type": "plain_text", "text": "Add secrets"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {
                "type": "input",
                "block_id": "paste_secrets__content",
                "label": {"type": "plain_text", "text": "KEY=VALUE lines"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "paste_secrets__content",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "XERO_API_KEY=abc123\nTOGGL_TOKEN=xyz789",
                    },
                },
            }
        ],
    }

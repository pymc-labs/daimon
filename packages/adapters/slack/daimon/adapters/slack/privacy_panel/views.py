"""Pure Block Kit view builders for the Slack privacy panel.

All functions return raw dicts — no slack_sdk imports, no core-store I/O.
Imports only stdlib (json, uuid) + the sibling mrkdwn escaper + core domain
types for type annotations.

Block Kit limits enforced:
  - Modal title ≤ 24 chars  (Pitfall 6)
  - private_metadata ≤ 3000 chars  (Pitfall 6)

Discord analogs:
  privacy_panel/panel.py:17-73 (_POLICY_URL, build_privacy_main_container)
  privacy_panel/cascade.py:18-85 (cascade preview body)
  privacy_panel/embeds.py (build_post_delete_container)
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.privacy import (
    DELETE_PAUSED,
    PRIVACY_TITLE,
    PurgePreview,
    delete_scope_lines,
    privacy_lines,
)
from daimon.core.purge import AccountPurgeResult

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cascade_blocks(preview: PurgePreview) -> list[dict[str, Any]]:
    """Build cascade preview section blocks.

    Port of privacy_panel/cascade.py (Discord) — two groups:
    what-will-be-deleted / what-is-kept-elsewhere.
    """
    will_happen_lines: list[str] = []

    def amount(count: int, singular: str, plural: str | None = None) -> str:
        return f"*{count}* {singular if count == 1 else plural or singular + 's'}"

    if preview.linked_principals.count > 0:
        ex = escape_mrkdwn(preview.linked_principals.example or "—")
        n = amount(preview.linked_principals.count, "linked account")
        will_happen_lines.append(f"• 🔑 Remove {n}, for example `{ex}`")
    if preview.routines.count > 0:
        will_happen_lines.append(f"• ⏰ Cancel {amount(preview.routines.count, 'routine')}")
    if preview.principal_links.count > 0:
        will_happen_lines.append(
            f"• 🔗 Remove {amount(preview.principal_links.count, 'link')} between your accounts"
        )
    if preview.user_configs.count > 0:
        will_happen_lines.append("• ⚙ Remove your saved settings")
    if preview.user_skills.count > 0:
        ex = escape_mrkdwn(preview.user_skills.example or "—")
        n = amount(preview.user_skills.count, "skill")
        will_happen_lines.append(f"• 🧰 Forget {n} you synced, for example `{ex}`")
    if preview.github_credentials.count > 0:
        ex = escape_mrkdwn(preview.github_credentials.example or "—")
        n = preview.github_credentials.count
        will_happen_lines.append(
            f"• 🔑 Delete {amount(n, 'saved GitHub key')}, for example the one for `{ex}`"
        )
    if preview.github_user_links.count > 0:
        will_happen_lines.append(
            f"• 🔗 Unlink {amount(preview.github_user_links.count, 'GitHub account')}"
        )
    if preview.github_oauth_states.count > 0:
        will_happen_lines.append(
            f"• 🤝 Delete {amount(preview.github_oauth_states.count, 'GitHub sign-in record')}"
        )
    if preview.mcp_tokens.count > 0:
        will_happen_lines.append(
            f"• 🎫 Delete {amount(preview.mcp_tokens.count, 'Daimon access token')}"
        )
    if preview.agent_github_binding.count > 0:
        n = preview.agent_github_binding.count
        will_happen_lines.append(f"• 🤖 Remove {amount(n, 'link')} from agents to your GitHub keys")
    if preview.slack_user_tokens.count > 0:
        will_happen_lines.append(
            f"• 🔐 Delete {amount(preview.slack_user_tokens.count, 'saved Slack access token')}"
        )
    if preview.slack_turn_contexts.count > 0:
        n = amount(preview.slack_turn_contexts.count, "temporary Slack request record")
        will_happen_lines.append(f"• 💬 Delete {n}")
    if preview.direct_message_conversations.count > 0:
        n = amount(preview.direct_message_conversations.count, "private conversation")
        will_happen_lines.append(f"• 💬 Delete Daimon's saved data for {n}")
    if preview.channel_admins.count > 0:
        will_happen_lines.append(
            f"• Remove you from {amount(preview.channel_admins.count, 'channel admin list')}"
        )
    if preview.account.count > 0:
        will_happen_lines.append("• 🪪 Remove your account")

    will_happen_text = "⚡ *What will happen*\n\n" + (
        "\n".join(will_happen_lines) if will_happen_lines else "_Nothing to delete._"
    )

    kept_text = (
        "📋 *What is intentionally kept elsewhere*\n\n"
        "• Usage records are retained for service integrity and cannot be erased on request.\n"
        "• Uploaded skill files stay in Managed Agents; guild agents may keep using them.\n"
        "• The GitHub-side OAuth authorization stays on your GitHub account"
        " — revoke it at github.com/settings/applications."
    )

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": will_happen_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": kept_text}},
    ]


# ---------------------------------------------------------------------------
# Public view builders
# ---------------------------------------------------------------------------


def build_loading_view() -> dict[str, Any]:
    """Lightweight loading modal opened immediately with the trigger_id.

    Shown while the background task fetches account + preview + is_admin.
    """
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Privacy"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Loading…"}},
        ],
    }


def build_delete_paused_view() -> dict[str, Any]:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Privacy"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": DELETE_PAUSED,
                },
            }
        ],
    }


def build_privacy_main_container(
    *,
    is_slack_connected: bool,
    slack_connect_url: str | None,
    policy_url: str,
    display_name: str = "daimon",
    delete_enabled: bool = True,
) -> dict[str, Any]:
    """Main privacy view: the title, two lines on who stores what, then the buttons.

    Port of privacy_panel/panel.py (Discord) adapted to Block Kit. The detail
    categories live in the policy behind the Policy button.

    The slack-token button reflects connection state: Disconnect when a user
    token is stored, a Connect url button (signed connect link) when not.
    ``slack_connect_url=None`` while disconnected (unmintable deploy) renders
    neither rather than a dead button.

    ``policy_url`` is the operator-configured privacy policy URL (from
    ``Settings.privacy_policy_url``). Pure function — caller passes the
    value in rather than importing config here (functional core).

    Returned dict is passed directly to views.update(view=...).
    """
    action_elements: list[dict[str, Any]] = [
        {
            "type": "button",
            "action_id": "privacy_policy",
            "text": {"type": "plain_text", "text": "📄 Policy"},
            "url": policy_url,
        },
        {
            "type": "button",
            "action_id": "privacy_export",
            "text": {"type": "plain_text", "text": "📤 Export"},
        },
    ]
    if delete_enabled:
        action_elements.append(
            {
                "type": "button",
                "action_id": "privacy_delete_open",
                "text": {"type": "plain_text", "text": "🗑 Delete…"},
                "style": "danger",
            }
        )
    if is_slack_connected:
        action_elements.append(
            {
                "type": "button",
                "action_id": "privacy_slack_disconnect",
                "text": {"type": "plain_text", "text": "🔌 Disconnect Slack"},
            }
        )
    elif slack_connect_url is not None:
        action_elements.append(_connect_button(slack_connect_url))

    body = "\n\n".join(escape_mrkdwn(line) for line in privacy_lines(display_name))
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{PRIVACY_TITLE}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {"type": "actions", "elements": action_elements},
    ]

    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Privacy"},
        "blocks": blocks,
    }


def build_delete_modal(
    preview: PurgePreview,
    *,
    account_id: uuid.UUID,
    user_name: str,
    view_id: str,
    display_name: str = "daimon",
) -> dict[str, Any]:
    """Single delete confirmation modal.

    Opens on what Delete removes and leaves (`delete_scope_lines`), then
    combines the cascade preview (what-will-be-deleted / kept-elsewhere)
    with a plain_text_input for typed-username confirmation in ONE modal.

    callback_id = "privacy_delete"; private_metadata carries account_id + user_name +
    view_id (≤3000 chars, Pitfall 6) so the view_submission handler can purge and
    update the right view without extra lookups.
    """
    private_metadata = json.dumps(
        {
            "account_id": str(account_id),
            "user_name": user_name,
            "view_id": view_id,
        },
        separators=(",", ":"),  # minimal whitespace to stay well under 3000 chars
    )

    scope = "\n\n".join(escape_mrkdwn(line) for line in delete_scope_lines(display_name))
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": scope}},
        {"type": "divider"},
        *_cascade_blocks(preview),
    ]
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "input",
            "block_id": "confirm_name_block",
            "label": {
                "type": "plain_text",
                "text": f"Type '{user_name}' to confirm",
            },
            "element": {
                "type": "plain_text_input",
                "action_id": "confirm_name",
                "placeholder": {"type": "plain_text", "text": user_name},
            },
        }
    )

    return {
        "type": "modal",
        "callback_id": "privacy_delete",
        "title": {"type": "plain_text", "text": "Confirm delete"},
        "submit": {"type": "plain_text", "text": "Delete"},
        "private_metadata": private_metadata,
        "blocks": blocks,
    }


def build_deleting_view() -> dict[str, Any]:
    """Transitional "Deleting…" modal view shown while purge_account runs.

    Returned via response_action="update" in the view_submission ack, then
    replaced by build_post_delete_view once the background purge completes.
    """
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Privacy"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "⏳ Deleting… this may take a moment.",
                },
            },
        ],
    }


def build_post_delete_view(
    result: AccountPurgeResult, *, display_name: str = "daimon"
) -> dict[str, Any]:
    """Final status modal view enumerating what was removed.

    Port of privacy_panel/embeds.py:15-71 (Discord) adapted to Block Kit.
    Always includes the carve-out disclosures (usage records, skill files,
    GitHub OAuth authorization).
    """
    rows: list[str] = []
    principals_total = result.db.platform_principals + result.db.cli_principals
    if principals_total > 0:
        rows.append(f"• ✓ {principals_total} linked principal(s) removed")
    if result.db.routines > 0:
        rows.append(f"• ✓ {result.db.routines} routine(s) cancelled")
    if result.db.principal_links > 0:
        rows.append(f"• ✓ {result.db.principal_links} principal link(s) removed")
    if result.db.user_configs > 0:
        rows.append(f"• ✓ {result.db.user_configs} user config row(s) removed")
    if result.db.user_skills > 0:
        rows.append(f"• ✓ {result.db.user_skills} synced skill ledger row(s) removed")
    if result.db.github_credentials > 0:
        rows.append(f"• ✓ {result.db.github_credentials} stored GitHub token(s) deleted")
    if result.db.github_user_links > 0:
        rows.append(f"• ✓ {result.db.github_user_links} GitHub user link(s) removed")
    if result.db.github_oauth_states > 0:
        rows.append(f"• ✓ {result.db.github_oauth_states} OAuth handshake record(s) removed")
    if result.db.accounts > 0:
        rows.append("• ✓ Account row removed")
    if result.sessions.deleted > 0:
        rows.append(f"• ✓ {result.sessions.deleted} session transcript(s) deleted from Anthropic")
    if result.sessions.upstream_error:
        rows.append("• We could not confirm deletion of all chat transcripts from Anthropic.")
        rows.append(
            f"• Ask the person who runs {escape_mrkdwn(display_name)} "
            "to check and help remove them."
        )
    elif result.sessions.failed > 0:
        count = result.sessions.failed
        noun = "chat transcript" if count == 1 else "chat transcripts"
        rows.append(f"• We could not delete {count} {noun} from Anthropic.")
        rows.append(f"• You do not need to retry while {escape_mrkdwn(display_name)} keeps trying.")
    # Carve-out disclosures — always shown.
    rows += [
        "",
        "• Usage records are retained for service integrity and cannot be erased on request.",
        "• Uploaded skill files stay in Managed Agents; guild agents may keep using them.",
        "• The GitHub-side OAuth authorization stays on your GitHub account"
        " — revoke it at github.com/settings/applications.",
    ]
    incomplete = result.sessions.failed > 0 or result.sessions.upstream_error
    if incomplete:
        introduction = (
            "*⚠ Deletion incomplete*\n"
            f"Your {escape_mrkdwn(display_name)} account was deleted, "
            "but chat transcript deletion is incomplete."
        )
    else:
        introduction = (
            "*✅ Account deleted*\n"
            f"Your {escape_mrkdwn(display_name)} account has been deleted.\n"
            f"You can start again by using {escape_mrkdwn(display_name)}."
        )

    return {
        "type": "modal",
        "title": {
            "type": "plain_text",
            "text": "⚠ Deletion incomplete" if incomplete else "✅ Account deleted",
        },
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": introduction,
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(rows)},
            },
        ],
    }


def _connect_button(connect_url: str) -> dict[str, Any]:
    """Connect/Reconnect url button — navigation is client-side; the
    block_action it also emits is intentionally undispatched."""
    return {
        "type": "button",
        "action_id": "privacy_slack_connect",
        "text": {"type": "plain_text", "text": "🔌 Connect Slack"},
        "url": connect_url,
    }


def build_export_result_view(
    *, summary: str | None, display_name: str = "daimon"
) -> dict[str, Any]:
    """Modal pushed after the Export action.

    The privacy panel's buttons live in a modal, whose block_actions payloads
    carry no channel — so the summary is pushed as a stacked modal rather than
    posted as an ephemeral channel message. ``summary=None`` means no account.
    """
    if summary is None:
        text = f"📤 *Privacy export*\nYou have no {escape_mrkdwn(display_name)} account."
    else:
        text = (
            "📤 *Privacy export (summary)*\n"
            f"{escape_mrkdwn(display_name)} holds: {summary}\n\n"
            "_Full JSON export is not yet implemented. When ready, it will produce "
            f"a download of every row {escape_mrkdwn(display_name)} stores for your identity._"
        )
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Export"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


def build_disconnect_result_view(
    *, was_connected: bool, reconnect_url: str | None, display_name: str = "daimon"
) -> dict[str, Any]:
    """Modal shown after the Disconnect Slack action (both outcomes).

    ``reconnect_url`` (the signed connect link) renders a Reconnect button so
    the user isn't stranded until they next hit an unreadable channel; None
    (unmintable deploy) falls back to prose-only.
    """
    if was_connected:
        text = (
            "*🔌 Slack account disconnected.*\n"
            f"{escape_mrkdwn(display_name)} no longer holds a token that reads Slack as you; "
            "reads fall "
            "back to channels the bot is invited to."
        )
    else:
        text = "*🔌 Nothing to disconnect.*\nYour Slack account was not connected."
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if reconnect_url is not None:
        blocks.append({"type": "actions", "elements": [_connect_button(reconnect_url)]})
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Privacy"},
        "blocks": blocks,
    }

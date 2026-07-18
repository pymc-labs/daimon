"""Pure Block Kit view builders for the Slack privacy panel.

All functions return raw dicts — no slack_sdk imports, no core-store I/O.
Imports only stdlib (json, uuid) + the sibling mrkdwn escaper + core domain
types for type annotations.

Block Kit limits enforced:
  - Modal title ≤ 24 chars  (Pitfall 6)
  - private_metadata ≤ 3000 chars  (Pitfall 6)

Discord analogs:
  privacy_panel/panel.py:17-73 (_POLICY_URL, _summary_line, build_privacy_main_container)
  privacy_panel/cascade.py:18-85 (cascade preview body)
  privacy_panel/embeds.py (build_post_delete_container)
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.privacy import PurgePreview
from daimon.core.purge import AccountPurgeResult

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def summary_line(preview: PurgePreview) -> str:
    """Return a comma-joined one-line summary of all non-zero held-data categories.

    Port of privacy_panel/panel.py:20-35 (Discord).
    """
    parts: list[str] = []
    if preview.linked_principals.count > 0:
        parts.append(f"{preview.linked_principals.count} linked principal(s)")
    if preview.routines.count > 0:
        parts.append(f"{preview.routines.count} routine(s)")
    if preview.user_configs.count > 0:
        parts.append(f"{preview.user_configs.count} user config row(s)")
    if preview.user_skills.count > 0:
        parts.append(f"{preview.user_skills.count} synced skill(s)")
    if preview.github_credentials.count > 0:
        parts.append(f"{preview.github_credentials.count} GitHub credential(s)")
    if preview.github_oauth_states.count > 0:
        parts.append(f"{preview.github_oauth_states.count} OAuth handshake record(s)")
    return ", ".join(parts) if parts else "nothing visible to you yet"


def _cascade_blocks(preview: PurgePreview) -> list[dict[str, Any]]:
    """Build cascade preview section blocks.

    Port of privacy_panel/cascade.py:25-75 (Discord) — three groups:
    what-will-be-deleted / what-stays-in-MA / what-is-kept-elsewhere.
    """
    will_happen_lines: list[str] = []
    if preview.linked_principals.count > 0:
        ex = escape_mrkdwn(preview.linked_principals.example or "—")
        will_happen_lines.append(
            f"• 🔑 Remove *{preview.linked_principals.count}* linked principal(s) (e.g. `{ex}`)"
        )
    if preview.routines.count > 0:
        ex = escape_mrkdwn(preview.routines.example or "—")
        will_happen_lines.append(
            f"• ⏰ Cancel *{preview.routines.count}* scheduled routine(s) (e.g. `{ex}`)"
        )
    if preview.principal_links.count > 0:
        will_happen_lines.append(f"• 🔗 Remove *{preview.principal_links.count}* principal link(s)")
    if preview.user_configs.count > 0:
        will_happen_lines.append(f"• ⚙ Remove *{preview.user_configs.count}* user config row(s)")
    if preview.user_skills.count > 0:
        ex = escape_mrkdwn(preview.user_skills.example or "—")
        will_happen_lines.append(
            f"• 🧰 Remove *{preview.user_skills.count}* synced skill ledger row(s) (e.g. `{ex}`)"
        )
    if preview.github_credentials.count > 0:
        ex = escape_mrkdwn(preview.github_credentials.example or "—")
        n = preview.github_credentials.count
        will_happen_lines.append(f"• 🔑 Delete *{n}* stored GitHub credential(s) (`{ex}`)")
    if preview.github_oauth_states.count > 0:
        will_happen_lines.append(
            f"• 🤝 Remove *{preview.github_oauth_states.count}* GitHub OAuth handshake record(s)"
        )
    if preview.account.count > 0:
        will_happen_lines.append("• 🪪 Remove the account row itself")

    will_happen_text = "⚡ *What will happen*\n" + (
        "\n".join(will_happen_lines) if will_happen_lines else "_(nothing to delete)_"
    )

    stays_text = (
        "🔐 *What stays in Managed Agents*\n"
        "• Agent definitions, system prompts, MCP credentials\n"
        "• Session transcripts, turn message content\n"
        "• Skill repo references — the repos themselves stay on GitHub\n"
        "• Retention is governed by Anthropic's Managed Agents policy."
    )

    kept_text = (
        "📋 *What is intentionally kept elsewhere*\n"
        "• Usage records are retained for service integrity and cannot be erased on request.\n"
        "• Uploaded skill files stay in Managed Agents; guild agents may keep using them.\n"
        "• The GitHub-side OAuth authorization stays on your GitHub account"
        " — revoke it at github.com/settings/applications."
    )

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": will_happen_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": stays_text}},
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


def build_privacy_main_container(
    preview: PurgePreview,
    *,
    is_slack_connected: bool,
    slack_connect_url: str | None,
    policy_url: str,
) -> dict[str, Any]:
    """Main privacy view: held-data summary + three trust-model groups + action buttons.

    Port of privacy_panel/panel.py:38-73 (Discord) adapted to Block Kit.

    The slack-token button reflects connection state: Disconnect when a user
    token is stored, a Connect url button (signed connect link) when not.
    ``slack_connect_url=None`` while disconnected (unmintable deploy) renders
    neither rather than a dead button.

    ``policy_url`` is the operator-configured privacy policy URL (from
    ``Settings.privacy_policy_url``). Pure function — caller passes the
    value in rather than importing config here (functional core).

    Returned dict is passed directly to views.update(view=...).
    """
    summary = escape_mrkdwn(summary_line(preview))

    body_text = (
        "🪪 *What we hold (our DB)*\n"
        "• Identity links (Slack/CLI principals under your account)\n"
        "• Routines you scheduled\n"
        "• User config rows\n"
        "• Synced skill ledger rows\n"
        "• Encrypted GitHub credentials (stored encrypted-at-rest)\n"
        "• GitHub OAuth handshake records\n"
        "• The account row itself\n"
        "\n"
        "🔐 *What lives in Managed Agents*\n"
        "• Agent definitions, system prompts, MCP credentials\n"
        "• Session transcripts, turn message content\n"
        "• Skill repo references (repos themselves stay on GitHub)\n"
        "• Retention governed by Anthropic's Managed Agents policy.\n"
        "\n"
        "🚫 *What we don't hold*\n"
        "• Plaintext credentials (GitHub tokens are encrypted-at-rest)\n"
        "• Message content (we only log structural events)"
    )

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
        {
            "type": "button",
            "action_id": "privacy_delete_open",
            "text": {"type": "plain_text", "text": "🗑 Delete…"},
            "style": "danger",
        },
    ]
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

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*🔒 Privacy*\ndaimon holds: {summary}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body_text},
        },
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
) -> dict[str, Any]:
    """Single delete confirmation modal.

    Combines the cascade preview (what-will-be-deleted / stays-in-MA / kept-elsewhere)
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

    blocks: list[dict[str, Any]] = _cascade_blocks(preview)
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


def build_post_delete_view(result: AccountPurgeResult) -> dict[str, Any]:
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
        rows.append(f"• ✓ {result.db.github_credentials} stored GitHub credential(s) deleted")
    if result.db.github_oauth_states > 0:
        rows.append(f"• ✓ {result.db.github_oauth_states} OAuth handshake record(s) removed")
    if result.db.accounts > 0:
        rows.append("• ✓ Account row removed")
    if result.sessions.deleted > 0:
        rows.append(f"• ✓ {result.sessions.deleted} session transcript(s) deleted from Anthropic")
    if result.sessions.failed > 0:
        rows.append(
            f"• ⚠ {result.sessions.failed} transcript(s) could not be deleted"
            " — re-run /privacy to retry"
        )
    if result.sessions.upstream_error:
        rows.append(
            "• ⚠ Session transcripts could not be deleted from Anthropic"
            " — contact the operator if you need them removed"
        )
    # Carve-out disclosures — always shown.
    rows += [
        "",
        "• Usage records are retained for service integrity and cannot be erased on request.",
        "• Uploaded skill files stay in Managed Agents; guild agents may keep using them.",
        "• The GitHub-side OAuth authorization stays on your GitHub account"
        " — revoke it at github.com/settings/applications.",
    ]

    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Deleted"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*✅ Deleted*\n"
                        "Your daimon data has been deleted. Re-onboarding starts from scratch."
                    ),
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


def build_export_result_view(*, summary: str | None) -> dict[str, Any]:
    """Modal pushed after the Export action.

    The privacy panel's buttons live in a modal, whose block_actions payloads
    carry no channel — so the summary is pushed as a stacked modal rather than
    posted as an ephemeral channel message. ``summary=None`` means no account.
    """
    if summary is None:
        text = "📤 *Privacy export*\nYou have no data on file with daimon."
    else:
        text = (
            "📤 *Privacy export (summary)*\n"
            f"daimon holds: {summary}\n\n"
            "_Full JSON export is not yet implemented. When ready, it will produce "
            "a download of every daimon-side row tied to your identity._"
        )
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Export"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


def build_disconnect_result_view(
    *, was_connected: bool, reconnect_url: str | None
) -> dict[str, Any]:
    """Modal shown after the Disconnect Slack action (both outcomes).

    ``reconnect_url`` (the signed connect link) renders a Reconnect button so
    the user isn't stranded until they next hit an unreadable channel; None
    (unmintable deploy) falls back to prose-only.
    """
    if was_connected:
        text = (
            "*🔌 Slack account disconnected.*\n"
            "daimon no longer holds a token that reads Slack as you; reads fall "
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

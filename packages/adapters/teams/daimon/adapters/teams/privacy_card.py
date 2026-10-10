"""Pure Adaptive Card builders for the `privacy` panel. No I/O.

Buttons are `Action.Execute` carrying `{"action": VERB, "op": ...}`; the delete
confirmation also carries the account it was rendered for, and the typed name
arrives as the `confirm_name` input.
"""

from __future__ import annotations

import uuid

from daimon.adapters.teams.card_actions import button, error_text, heading, text_card, text_lines
from daimon.core.privacy import PurgePreview, summary_line
from daimon.core.purge import AccountPurgeResult
from microsoft_teams.cards import (
    Action,
    ActionSet,
    AdaptiveCard,
    CardElement,
    ExecuteAction,
    OpenUrlAction,
    TextInput,
)

VERB = "privacy"
CONFIRM_INPUT = "confirm_name"

_HOLD = (
    "🪪 **What we hold (our DB)**",
    "Identity links (Teams and CLI principals under your account), routines you scheduled, "
    "user config rows, synced skill ledger rows, encrypted GitHub tokens, GitHub OAuth "
    "handshake records and the account row itself.",
)
_MANAGED_AGENTS = (
    "🔐 **What lives in Managed Agents**",
    "Agent definitions, system prompts and MCP tokens; session transcripts and turn message "
    "content; skill repo references (the repos stay on GitHub). Retention is governed by "
    "Anthropic's Managed Agents policy.",
)
_NOT_HELD = (
    "🚫 **What we don't hold**",
    "Plaintext keys or tokens (GitHub tokens are encrypted at rest), or message content "
    "(we only log structural events).",
)
_KEPT = (
    "📋 **What is intentionally kept elsewhere**",
    "Usage records are retained for service integrity and cannot be erased on request. "
    "Uploaded skill files stay in Managed Agents; shared agents may keep using them. "
    "The GitHub-side OAuth authorization stays on your GitHub account; revoke it at "
    "github.com/settings/applications.",
)


def back() -> ExecuteAction:
    return button(VERB, "Back", "refresh")


def no_data_card(bot: str) -> AdaptiveCard:
    return text_card("🔒 Privacy", f"You have no {bot} account.")


def panel_card(preview: PurgePreview, *, bot: str, policy_url: str) -> AdaptiveCard:
    """What is held and where, with Policy, Export and Delete."""
    actions: list[Action] = [
        OpenUrlAction(title="📄 Policy", url=policy_url),
        button(VERB, "📤 Export", "export"),
        button(VERB, "🗑 Delete…", "delete", style="destructive"),
    ]
    body: list[CardElement] = [
        heading("🔒 Privacy"),
        *text_lines(f"{bot} holds: {summary_line(preview)}", *_HOLD, *_MANAGED_AGENTS, *_NOT_HELD),
        ActionSet(actions=actions),
    ]
    return AdaptiveCard(body=body, fallback_text="Privacy")


def export_card(preview: PurgePreview, *, bot: str) -> AdaptiveCard:
    return text_card(
        "📤 Privacy export (summary)",
        f"{bot} holds: {summary_line(preview)}",
        f"A full JSON export is not yet implemented. When ready, it will produce a download "
        f"of every row {bot} stores for your identity.",
        back=back(),
    )


def _will_happen(preview: PurgePreview) -> list[str]:
    rows = (
        ("🔑 Remove", preview.linked_principals, "linked principal(s)"),
        ("⏰ Cancel", preview.routines, "scheduled routine(s)"),
        ("🔗 Remove", preview.principal_links, "principal link(s)"),
        ("⚙ Remove", preview.user_configs, "user config row(s)"),
        ("🧰 Remove", preview.user_skills, "synced skill ledger row(s)"),
        ("🔑 Delete", preview.github_credentials, "stored GitHub token(s)"),
        ("🔗 Remove", preview.github_user_links, "GitHub user link(s)"),
        ("🤝 Remove", preview.github_oauth_states, "GitHub OAuth handshake record(s)"),
        ("🎫 Revoke", preview.mcp_tokens, "per-agent MCP token(s)"),
        ("🤖 Remove", preview.agent_github_binding, "per-agent GitHub token link(s)"),
        ("🔐 Remove", preview.slack_user_tokens, "Slack user token(s)"),
        ("💬 Remove", preview.slack_turn_contexts, "Slack turn context(s)"),
        ("💬 Remove", preview.direct_message_conversations, "private conversation(s)"),
    )
    lines = [
        f"{verb} **{row.count}** {label}" + (f" (e.g. {row.example})" if row.example else "")
        for verb, row, label in rows
        if row.count > 0
    ]
    if preview.account.count > 0:
        lines.append("🪪 Remove the account row itself")
    return lines or ["(nothing to delete)"]


def confirm_card(
    preview: PurgePreview, *, account_id: uuid.UUID, name: str, error: str | None = None
) -> AdaptiveCard:
    """What deleting removes and keeps, then a typed-name confirmation."""
    body: list[CardElement] = [heading("Confirm delete")]
    if error:
        body.append(error_text(error))
    body += text_lines("⚡ **What will happen**", *_will_happen(preview), *_MANAGED_AGENTS, *_KEPT)
    body.append(TextInput(id=CONFIRM_INPUT, label=f"Type '{name}' to confirm", placeholder=name))
    delete = button(VERB, "Delete", "confirm_delete", style="destructive", account=str(account_id))
    body.append(ActionSet(actions=[delete, button(VERB, "Cancel", "refresh")]))
    return AdaptiveCard(body=body, fallback_text="Confirm delete")


def post_delete_card(result: AccountPurgeResult, *, bot: str) -> AdaptiveCard:
    """What the purge removed, plus the carve-outs that always apply."""
    db, sessions = result.db, result.sessions
    rows = (
        (db.platform_principals + db.cli_principals, "linked principal(s) removed"),
        (db.routines, "routine(s) cancelled"),
        (db.principal_links, "principal link(s) removed"),
        (db.user_configs, "user config row(s) removed"),
        (db.user_skills, "synced skill ledger row(s) removed"),
        (db.github_credentials, "stored GitHub token(s) deleted"),
        (db.github_user_links, "GitHub user link(s) removed"),
        (db.github_oauth_states, "OAuth handshake record(s) removed"),
        (db.accounts, "account row removed"),
        (sessions.deleted, "session transcript(s) deleted from Anthropic"),
    )
    lines = [f"✓ {count} {label}" for count, label in rows if count > 0]
    if sessions.upstream_error:
        lines.append("We could not confirm deletion of all chat transcripts from Anthropic.")
        lines.append(f"Ask the person who runs {bot} to check and help remove them.")
    elif sessions.failed > 0:
        noun = "chat transcript" if sessions.failed == 1 else "chat transcripts"
        lines.append(f"We could not delete {sessions.failed} {noun} from Anthropic.")
        lines.append(f"You do not need to retry while {bot} keeps trying.")
    return text_card(
        "⚠ Deletion incomplete"
        if sessions.failed > 0 or sessions.upstream_error
        else "✅ Account deleted",
        (
            f"Your {bot} account was deleted, but chat transcript deletion is incomplete."
            if sessions.failed > 0 or sessions.upstream_error
            else f"Your {bot} account has been deleted."
        ),
        *(
            []
            if sessions.failed > 0 or sessions.upstream_error
            else [f"You can start again by using {bot}."]
        ),
        *lines,
        _KEPT[1],
    )

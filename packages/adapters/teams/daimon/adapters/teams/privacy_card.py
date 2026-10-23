"""Pure Adaptive Card builders for the `privacy` panel. No I/O.

Buttons are `Action.Execute` carrying `{"action": VERB, "op": ...}`; the delete
confirmation also carries the account it was rendered for, and the typed name
arrives as the `confirm_name` input.
"""

from __future__ import annotations

import uuid

from daimon.adapters.teams.card_actions import button, error_text, heading, text_card, text_lines
from daimon.core.privacy import (
    PRIVACY_TITLE,
    PurgePreview,
    delete_scope_lines,
    preview_example,
    privacy_lines,
    summary_line,
)
from daimon.core.purge import AccountPurgeResult
from microsoft_teams.cards import (
    Action,
    ActionSet,
    AdaptiveCard,
    CardElement,
    ExecuteAction,
    OpenUrlAction,
    TextBlock,
    TextInput,
)

VERB = "privacy"
CONFIRM_INPUT = "confirm_name"

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


def _spaced(*lines: str) -> list[CardElement]:
    """Wrapped lines, each a blank line's gap below the one before."""
    return [TextBlock(text=line, wrap=True, spacing="Medium") for line in lines]


def panel_card(
    *, bot: str, policy_url: str, delete_enabled: bool = True, microsoft_connected: bool = False
) -> AdaptiveCard:
    """Who stores what, with Policy, Export and Delete; the detail is the policy's."""
    actions: list[Action] = [
        OpenUrlAction(title="📄 Policy", url=policy_url),
        button(VERB, "📤 Export", "export"),
    ]
    if microsoft_connected:
        actions.append(button(VERB, "🔌 Disconnect Microsoft", "disconnect_microsoft"))
    if delete_enabled:
        actions.append(button(VERB, "🗑 Delete…", "delete", style="destructive"))
    body: list[CardElement] = [
        heading(PRIVACY_TITLE),
        *_spaced(*privacy_lines(bot)),
        ActionSet(actions=actions, spacing="Medium"),
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
    def amount(count: int, singular: str, plural: str | None = None) -> str:
        return f"**{count}** {singular if count == 1 else plural or singular + 's'}"

    lines: list[str] = []
    if preview.linked_principals.count:
        n = amount(preview.linked_principals.count, "linked account")
        ex = preview_example(preview.linked_principals)
        lines.append(f"🔑 Remove {n}, for example {ex}")
    if preview.routines.count:
        lines.append(f"⏰ Cancel {amount(preview.routines.count, 'routine')}")
    if preview.principal_links.count:
        lines.append(
            f"🔗 Remove {amount(preview.principal_links.count, 'link')} between your accounts"
        )
    if preview.user_configs.count:
        lines.append("⚙ Remove your saved settings")
    if preview.user_skills.count:
        n = amount(preview.user_skills.count, "skill")
        ex = preview_example(preview.user_skills)
        lines.append(f"🧰 Forget {n} you synced, for example {ex}")
    if preview.github_connect_requests.count:
        n = amount(preview.github_connect_requests.count, "GitHub setup request")
        lines.append(f"🔑 Delete {n}")
    if preview.github_connect_click_intents.count:
        n = amount(preview.github_connect_click_intents.count, "GitHub connect button")
        lines.append(f"🔑 Delete {n}")
    if preview.github_user_links.count:
        lines.append(f"🔗 Unlink {amount(preview.github_user_links.count, 'GitHub account')}")
    if preview.mcp_tokens.count:
        lines.append(f"🎫 Delete {amount(preview.mcp_tokens.count, 'Daimon access token')}")
    if preview.gemini_records.count:
        n = amount(preview.gemini_records.count, "Gemini conversation record")
        lines.append(f"💬 Delete {n}")
    if preview.slack_user_tokens.count:
        lines.append(f"🔐 Delete {amount(preview.slack_user_tokens.count, 'saved access token')}")
    if preview.microsoft_user_tokens.count:
        lines.append("🔌 Disconnect your Microsoft account")
    if preview.slack_turn_contexts.count:
        n = amount(preview.slack_turn_contexts.count, "temporary request record")
        lines.append(f"💬 Delete {n}")
    if preview.direct_message_conversations.count:
        n = amount(preview.direct_message_conversations.count, "private conversation")
        lines.append(f"💬 Delete Daimon's saved data for {n}")
    if preview.credential_requests.count:
        n = amount(preview.credential_requests.count, "credential request")
        lines.append(f"Delete {n}")
    if preview.wizard_sessions.count:
        n = amount(preview.wizard_sessions.count, "wizard session")
        lines.append(f"Delete {n}")
    if preview.message_feedback.count:
        n = amount(preview.message_feedback.count, "message feedback record")
        lines.append(f"Delete {n}")
    if preview.support_escalations.count:
        n = amount(preview.support_escalations.count, "support escalation")
        lines.append(f"Delete {n}")
    if preview.channel_admins.count:
        lines.append(
            f"Remove you from {amount(preview.channel_admins.count, 'channel admin list')}"
        )
    if preview.agent_post_requesters.count:
        n = amount(
            preview.agent_post_requesters.count,
            "post an agent made for you",
            "posts agents made for you",
        )
        lines.append(f"Forget that you requested {n}; keep the posts")
    if preview.platform_user_names.count:
        n = amount(preview.platform_user_names.count, "remembered name")
        lines.append(f"Delete {n}")
    if preview.teams_messages.count:
        n = amount(preview.teams_messages.count, "Teams message record")
        lines.append(f"Delete {n}")
    if preview.account.count > 0:
        lines.append("🪪 Remove your account")
    return lines or ["Nothing to delete."]


def confirm_card(
    preview: PurgePreview,
    *,
    account_id: uuid.UUID,
    name: str,
    bot: str = "daimon",
    error: str | None = None,
) -> AdaptiveCard:
    """What deleting removes and keeps, then a typed-name confirmation."""
    body: list[CardElement] = [heading("Confirm delete")]
    if error:
        body.append(error_text(error))
    body += _spaced(*delete_scope_lines(bot))
    body += _spaced("⚡ **What will happen**")
    body += text_lines(*_will_happen(preview))
    body += _spaced(*_KEPT)
    body.append(TextInput(id=CONFIRM_INPUT, label=f"Type '{name}' to confirm", placeholder=name))
    delete = button(VERB, "Delete", "confirm_delete", style="destructive", account=str(account_id))
    body.append(ActionSet(actions=[delete, button(VERB, "Cancel", "refresh")]))
    return AdaptiveCard(body=body, fallback_text="Confirm delete")


def post_delete_card(result: AccountPurgeResult, *, bot: str) -> AdaptiveCard:
    """What the purge removed, plus the carve-outs that always apply."""
    db, sessions = result.db, result.sessions
    rows = (
        (db.platform_principals + db.cli_principals, "linked account(s) removed"),
        (db.routines, "routine(s) cancelled"),
        (db.principal_links, "link(s) between your accounts removed"),
        (db.user_configs, "saved setting(s) removed"),
        (db.user_skills, "synced skill(s) forgotten"),
        (db.github_user_links, "GitHub account(s) unlinked"),
        (db.microsoft_user_tokens, "Microsoft connection removed"),
        (db.accounts, "account removed"),
        (sessions.deleted, "conversation transcript(s) deleted from Anthropic"),
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


def microsoft_disconnected_card() -> AdaptiveCard:
    return text_card(
        "🔌 Microsoft disconnected",
        "Daimon no longer holds your Microsoft sign-in, so it cannot reach your files, mail, "
        "calendar or notes. Ask it to connect Microsoft again any time.",
        back=back(),
    )

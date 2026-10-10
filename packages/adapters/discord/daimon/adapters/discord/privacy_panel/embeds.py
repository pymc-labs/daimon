"""Post-delete and deleted-state container builders. No views attached.

Post-delete green (theme.COLOR_GREEN), deleted-state greyple
(theme.COLOR_GREYPLE). Both are controls-less by construction (no ActionRows).
"""

from __future__ import annotations

from daimon.adapters.discord import layout, theme
from daimon.core.purge import AccountPurgeResult

import discord


def build_post_delete_container(
    result: AccountPurgeResult, *, bot_display_name: str = "daimon"
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Green-accent V2 container shown after a successful purge. Controls-less."""
    rows: list[str] = []
    principals_total = result.db.platform_principals + result.db.cli_principals
    if principals_total > 0:
        rows.append(f"-# ✓ {principals_total} linked principal(s) removed")
    if result.db.routines > 0:
        rows.append(f"-# ✓ {result.db.routines} routine(s) cancelled")
    if result.db.principal_links > 0:
        rows.append(f"-# ✓ {result.db.principal_links} principal link(s) removed")
    if result.db.user_configs > 0:
        rows.append(f"-# ✓ {result.db.user_configs} user config row(s) removed")
    if result.db.user_skills > 0:
        rows.append(f"-# ✓ {result.db.user_skills} synced skill ledger row(s) removed")
    if result.db.github_credentials > 0:
        rows.append(f"-# ✓ {result.db.github_credentials} stored GitHub token(s) deleted")
    if result.db.github_user_links > 0:
        rows.append(f"-# ✓ {result.db.github_user_links} GitHub user link(s) removed")
    if result.db.github_oauth_states > 0:
        rows.append(f"-# ✓ {result.db.github_oauth_states} OAuth handshake record(s) removed")
    if result.db.mcp_tokens > 0:
        rows.append(f"-# ✓ {result.db.mcp_tokens} per-agent MCP token(s) revoked")
    if result.db.agent_github_binding > 0:
        rows.append(f"-# ✓ {result.db.agent_github_binding} per-agent GitHub token link(s) removed")
    if result.db.accounts > 0:
        rows.append("-# ✓ Account row removed")
    if result.sessions.deleted > 0:
        rows.append(f"-# ✓ {result.sessions.deleted} session transcript(s) deleted from Anthropic")
    if result.sessions.upstream_error:
        rows.append("-# We could not confirm deletion of all chat transcripts from Anthropic.")
        rows.append(f"-# Ask the person who runs {bot_display_name} to check and help remove them.")
    elif result.sessions.failed > 0:
        count = result.sessions.failed
        noun = "chat transcript" if count == 1 else "chat transcripts"
        rows.append(f"-# We could not delete {count} {noun} from Anthropic.")
        rows.append(f"-# You do not need to retry while {bot_display_name} keeps trying.")
    # Carve-out disclosures — always shown (same three as cascade-preview).
    rows += [
        "",
        "-# Usage records are retained for service integrity and cannot be erased on request.",
        "-# Uploaded skill files stay in Managed Agents; guild agents may keep using them.",
        "-# The GitHub-side OAuth authorization stays on your GitHub account"
        " — revoke it at github.com/settings/applications.",
    ]
    # rows is never empty — the carve-out rows above are unconditional.
    checklist = "\n".join(rows)
    incomplete = result.sessions.failed > 0 or result.sessions.upstream_error
    if incomplete:
        title = "⚠ Deletion incomplete"
        subtext = (
            f"Your {bot_display_name} account was deleted, "
            "but chat transcript deletion is incomplete."
        )
    else:
        title = "✅ Account deleted"
        subtext = (
            f"Your {bot_display_name} account has been deleted.\n"
            f"-# You can start again by using {bot_display_name}."
        )
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        layout.header(
            title,
            subtext=subtext,
        ),
        layout.hairline(),
        discord.ui.TextDisplay(checklist),
        accent_colour=theme.COLOR_GREEN,
    )
    return container


def build_deleted_state_container(
    user_name: str,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Grey-accent V2 container shown when /privacy is re-run after delete or no data."""
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        layout.header(
            "🔒 Privacy",
            subtext=f"for **{user_name}**",
        ),
        layout.hairline(),
        discord.ui.TextDisplay("You have no Daimon account."),
        accent_colour=theme.COLOR_GREYPLE,
    )
    return container

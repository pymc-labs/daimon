"""Post-delete and deleted-state container builders. No views attached.

Post-delete green (theme.COLOR_GREEN), deleted-state greyple
(theme.COLOR_GREYPLE). Both are controls-less by construction (no ActionRows).
"""

from __future__ import annotations

from daimon.adapters.discord import layout, theme
from daimon.core.purge import AccountPurgeResult

import discord


def build_post_delete_container(
    result: AccountPurgeResult,
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
        rows.append(f"-# ✓ {result.db.github_credentials} stored GitHub credential(s) deleted")
    if result.db.github_oauth_states > 0:
        rows.append(f"-# ✓ {result.db.github_oauth_states} OAuth handshake record(s) removed")
    if result.db.mcp_tokens > 0:
        rows.append(f"-# ✓ {result.db.mcp_tokens} per-agent MCP token(s) revoked")
    if result.db.agent_github_binding > 0:
        rows.append(
            f"-# ✓ {result.db.agent_github_binding} per-agent GitHub credential link(s) removed"
        )
    if result.db.accounts > 0:
        rows.append("-# ✓ Account row removed")
    if result.sessions.deleted > 0:
        rows.append(f"-# ✓ {result.sessions.deleted} session transcript(s) deleted from Anthropic")
    if result.sessions.failed > 0:
        rows.append(
            f"-# ⚠ {result.sessions.failed} transcript(s) could not be deleted"
            " — re-run /privacy to retry"
        )
    if result.sessions.upstream_error:
        # Post-commit upstream failure: the DB purge completed, but session
        # enumeration/deletion aborted. No /privacy retry hint — the account
        # row is gone, so /privacy now renders the deleted state.
        rows.append(
            "-# ⚠ Session transcripts could not be deleted from Anthropic"
            " — contact the operator if you need them removed"
        )
    # D-09 carve-out disclosures — always shown (same three as cascade-preview).
    rows += [
        "",
        "-# Usage records are retained for service integrity and cannot be erased on request.",
        "-# Uploaded skill files stay in Managed Agents; guild agents may keep using them.",
        "-# The GitHub-side OAuth authorization stays on your GitHub account"
        " — revoke it at github.com/settings/applications.",
    ]
    # rows is never empty — the D-09 carve-out rows above are unconditional.
    checklist = "\n".join(rows)
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        layout.header(
            "✅ Deleted",
            subtext="Your daimon data has been deleted. Re-onboarding starts from scratch.",
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
            subtext=f"for **{user_name}** — no data on file",
        ),
        layout.hairline(),
        discord.ui.TextDisplay(
            "You have no data on file with daimon.\n-# Run any other slash command to start fresh."
        ),
        accent_colour=theme.COLOR_GREYPLE,
    )
    return container

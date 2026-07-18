"""build_post_delete_container + build_deleted_state_container tests."""

from __future__ import annotations

import discord
from daimon.adapters.discord import theme
from daimon.adapters.discord.privacy_panel.embeds import (
    build_deleted_state_container,
    build_post_delete_container,
)
from daimon.core.ma import SessionDeletionReport
from daimon.core.purge import AccountPurgeResult, PurgeReport


def _text_displays(container: discord.ui.Container[discord.ui.LayoutView]) -> list[str]:
    """Collect content from all TextDisplay children in the container."""
    return [item.content for item in container.children if isinstance(item, discord.ui.TextDisplay)]


def _joined_text(container: discord.ui.Container[discord.ui.LayoutView]) -> str:
    return "\n".join(_text_displays(container))


def _make_result(**purge_kwargs: int) -> AccountPurgeResult:
    """Build an AccountPurgeResult with empty sessions and given PurgeReport fields."""
    return AccountPurgeResult(db=PurgeReport(**purge_kwargs))


def test_post_delete_container_is_green() -> None:
    """Post-delete container must have green accent (D-COLOR-01)."""
    result = AccountPurgeResult(
        db=PurgeReport(
            routines=2,
            principal_links=1,
            cli_principals=0,
            platform_principals=1,
            user_configs=1,
            accounts=1,
        )
    )
    container = build_post_delete_container(result)
    assert container.accent_colour == theme.COLOR_GREEN, (
        f"Post-delete container accent must be brand green 0x{theme.COLOR_GREEN:06X};"
        f" got {container.accent_colour}"
    )


def test_post_delete_container_header_starts_with_deleted() -> None:
    """First TextDisplay must start with '## ✅ Deleted'."""
    result = _make_result(accounts=1)
    container = build_post_delete_container(result)
    texts = _text_displays(container)
    assert texts, "Container must have at least one TextDisplay"
    assert texts[0].startswith("## ✅ Deleted"), (
        f"Post-delete header must start with '## ✅ Deleted'; got {texts[0]!r}"
    )


def test_post_delete_container_checklist_has_dim_rows() -> None:
    """Checklist rows must be -# prefixed (dim)."""
    result = AccountPurgeResult(
        db=PurgeReport(
            routines=2,
            platform_principals=1,
            principal_links=1,
            accounts=1,
        )
    )
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "-# ✓" in joined, f"Post-delete checklist rows must use '-# ✓' prefix; got {joined!r}"
    assert "2 routine" in joined, "2 routines purged should appear"
    assert "1 linked principal" in joined, "1 principal purged should appear"
    assert "1 principal link" in joined, "1 principal link removed should appear"
    assert "Account row removed" in joined, "account-row removal should appear"


def test_post_delete_container_no_pii_beyond_header() -> None:
    """Checklist rows must contain counts only, not the user_name."""
    result = _make_result(accounts=1)
    container = build_post_delete_container(result)
    texts = _text_displays(container)
    # Skip the header TextDisplay; check remaining rows have no user_name
    checklist_texts = "\n".join(texts[1:]) if len(texts) > 1 else ""
    assert "carlos" not in checklist_texts, (
        f"Checklist rows must not contain user_name (counts only, no PII); got {checklist_texts!r}"
    )


def test_post_delete_container_reports_session_transcripts_deleted() -> None:
    """When sessions.deleted > 0 the container reports the count."""
    result = AccountPurgeResult(
        db=PurgeReport(accounts=1),
        sessions=SessionDeletionReport(deleted=3, failed=0),
    )
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "3 session transcript(s) deleted" in joined, (
        f"When sessions.deleted > 0, container must report the count; got {joined!r}"
    )


def test_post_delete_container_reports_session_transcript_failures() -> None:
    """When sessions.failed > 0 the container surfaces failures."""
    result = AccountPurgeResult(
        db=PurgeReport(accounts=1),
        sessions=SessionDeletionReport(deleted=2, failed=1),
    )
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "1 transcript(s) could not be deleted" in joined, (
        f"When sessions.failed > 0, container must surface failures; got {joined!r}"
    )


def test_post_delete_container_discloses_upstream_error_without_retry_hint() -> None:
    """When sessions.upstream_error is set, the container discloses that
    transcripts remain at Anthropic — without the unreachable /privacy retry hint
    (the account row is gone, so /privacy renders the deleted state)."""
    result = AccountPurgeResult(
        db=PurgeReport(accounts=1),
        sessions=SessionDeletionReport(deleted=0, failed=0, upstream_error=True),
    )
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "could not be deleted from Anthropic" in joined, (
        f"upstream_error must surface a transcript-deletion disclosure; got {joined!r}"
    )
    assert "re-run /privacy" not in joined, (
        "upstream_error disclosure must NOT promise a /privacy retry — it is unreachable "
        f"after the account row is purged; got {joined!r}"
    )


def test_post_delete_container_upstream_error_row_absent_when_clean() -> None:
    """No upstream_error row when the upstream phase completed."""
    result = AccountPurgeResult(
        db=PurgeReport(accounts=1),
        sessions=SessionDeletionReport(deleted=2, failed=0),
    )
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "could not be deleted from Anthropic" not in joined, (
        f"clean upstream phase must not render the upstream_error row; got {joined!r}"
    )


def test_post_delete_container_no_retention_legalese() -> None:
    """Container must not contain retention legalese."""
    result = _make_result(accounts=1)
    container = build_post_delete_container(result)
    joined = _joined_text(container).lower()
    for phrase in ("2 year", "2-year", "retention policy", "billing"):
        assert phrase not in joined, (
            f"Post-delete container must not contain retention legalese; found {phrase!r}"
        )


def test_post_delete_container_user_skills_row_renders_when_nonzero() -> None:
    """user_skills checklist row appears when count > 0."""
    result = _make_result(user_skills=3)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "3 synced skill ledger row(s) removed" in joined, (
        "When result.db.user_skills > 0, post-delete container must show the user_skills row"
    )


def test_post_delete_container_user_skills_row_absent_when_zero() -> None:
    """D-PREVIEW-FMT-01: user_skills row is suppressed when count == 0."""
    result = _make_result(user_skills=0, accounts=1)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "synced skill ledger" not in joined, (
        "zero-count user_skills must NOT render a checklist row (D-PREVIEW-FMT-01)"
    )


def test_post_delete_container_github_credentials_row_renders_when_nonzero() -> None:
    """github_credentials checklist row appears when count > 0."""
    result = _make_result(github_credentials=1)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "1 stored GitHub credential(s) deleted" in joined, (
        "When result.db.github_credentials > 0, post-delete container must show the github_credentials row"
    )


def test_post_delete_container_github_credentials_row_absent_when_zero() -> None:
    """D-PREVIEW-FMT-01: github_credentials row is suppressed when count == 0."""
    result = _make_result(github_credentials=0, accounts=1)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "GitHub credential" not in joined, (
        "zero-count github_credentials must NOT render a checklist row (D-PREVIEW-FMT-01)"
    )


def test_post_delete_container_github_oauth_states_row_renders_when_nonzero() -> None:
    """github_oauth_states checklist row appears when count > 0."""
    result = _make_result(github_oauth_states=2)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "2 OAuth handshake record(s) removed" in joined, (
        "When result.db.github_oauth_states > 0, post-delete container must show the oauth_states row"
    )


def test_post_delete_container_github_oauth_states_row_absent_when_zero() -> None:
    """D-PREVIEW-FMT-01: github_oauth_states row is suppressed when count == 0."""
    result = _make_result(github_oauth_states=0, accounts=1)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "OAuth handshake" not in joined, (
        "zero-count github_oauth_states must NOT render a checklist row (D-PREVIEW-FMT-01)"
    )


def test_post_delete_container_carveout_usage_records_always_present() -> None:
    """usage-records retention carve-out is always shown on the post-delete embed."""
    result = _make_result(accounts=1)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "service integrity" in joined, (
        "Post-delete container must disclose usage-records retention carve-out"
    )


def test_post_delete_container_carveout_ma_skill_files_always_present() -> None:
    """MA skill files carve-out is always shown on the post-delete embed."""
    result = _make_result(accounts=1)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "Managed Agents" in joined, (
        "Post-delete container must mention Managed Agents in skill-files carve-out"
    )


def test_post_delete_container_carveout_github_grant_always_present() -> None:
    """GitHub-side OAuth grant carve-out is always shown on the post-delete embed."""
    result = _make_result(accounts=1)
    container = build_post_delete_container(result)
    joined = _joined_text(container)
    assert "github.com/settings/applications" in joined, (
        "Post-delete container must direct users to revoke their GitHub OAuth grant"
    )


def test_deleted_state_container_is_grey() -> None:
    """Deleted-state container must have greyple accent (D-COLOR-01)."""
    container = build_deleted_state_container("carlos")
    assert container.accent_colour == theme.COLOR_GREYPLE, (
        f"Deleted-state accent must be greyple 0x{theme.COLOR_GREYPLE:06X};"
        f" got {container.accent_colour}"
    )


def test_deleted_state_container_has_no_data_on_file_copy() -> None:
    """Deleted-state container must contain 'no data on file' copy (D-COPY-02)."""
    container = build_deleted_state_container("carlos")
    joined = _joined_text(container)
    assert "no data on file" in joined, (
        f"Deleted-state copy must contain 'no data on file'; got {joined!r}"
    )


def test_deleted_state_container_header_no_warning_glyphs() -> None:
    """Deleted-state header must NOT use warning/error glyphs (D-COPY-02)."""
    container = build_deleted_state_container("carlos")
    texts = _text_displays(container)
    assert texts, "Container must have at least one TextDisplay"
    header_text = texts[0]
    assert "⚠" not in header_text and "❌" not in header_text, (
        "Deleted-state header must NOT use warning glyphs per D-COPY-02"
    )

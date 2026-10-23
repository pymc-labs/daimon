"""Every preview category is explained in each deletion card and export summary.

The schema partition is intentional: adding a field must add a user-facing
expectation or a documented exclusion. Fixtures start with every schema field
zero, including fields with defaults, so one category cannot hide another.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.discord.privacy_panel.cascade import (
    CascadePreviewView,
    build_cascade_preview_container,
)
from daimon.adapters.discord.privacy_panel.panel import _export_placeholder_message
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.slack.privacy_panel import views as slack_views
from daimon.adapters.slack.privacy_panel.views import (
    build_delete_modal,
    build_export_result_view,
)
from daimon.adapters.teams.privacy_card import confirm_card, export_card
from daimon.core.privacy import PurgePreview, PurgePreviewRow, summary_line
from microsoft_teams.api import Account, ConversationAccount, MessageActivityInput
from microsoft_teams.cards import AdaptiveCard, TextBlock

_ACCOUNT_ID = uuid.UUID(int=1)
_NAME = "synthetic-user"

# These are independently specified user-visible rows, not production metadata.
# Full phrases distinguish removal of attribution from deletion of the posts.
_COUNTED_CASCADE_ROWS = {
    "linked_principals": ("Remove {n} linked account", "Remove {n} linked accounts"),
    "principal_links": (
        "Remove {n} link between your accounts",
        "Remove {n} links between your accounts",
    ),
    "routines": ("Cancel {n} routine", "Cancel {n} routines"),
    "user_skills": ("Forget {n} skill you synced", "Forget {n} skills you synced"),
    "github_connect_requests": (
        "Delete {n} GitHub setup request",
        "Delete {n} GitHub setup requests",
    ),
    "github_connect_click_intents": (
        "Delete {n} GitHub connect button",
        "Delete {n} GitHub connect buttons",
    ),
    "github_user_links": ("Unlink {n} GitHub account", "Unlink {n} GitHub accounts"),
    "mcp_tokens": ("Delete {n} Daimon access token", "Delete {n} Daimon access tokens"),
    "gemini_records": (
        "Delete {n} Gemini conversation record",
        "Delete {n} Gemini conversation records",
    ),
    "slack_user_tokens": (
        "Delete {n} saved Slack access token",
        "Delete {n} saved Slack access tokens",
    ),
    "slack_turn_contexts": (
        "Delete {n} temporary Slack request record",
        "Delete {n} temporary Slack request records",
    ),
    "direct_message_conversations": (
        "Delete Daimon's saved data for {n} private conversation",
        "Delete Daimon's saved data for {n} private conversations",
    ),
    "credential_requests": ("Delete {n} credential request", "Delete {n} credential requests"),
    "wizard_sessions": ("Delete {n} wizard session", "Delete {n} wizard sessions"),
    "message_feedback": (
        "Delete {n} message feedback record",
        "Delete {n} message feedback records",
    ),
    "support_escalations": ("Delete {n} support escalation", "Delete {n} support escalations"),
    "channel_admins": (
        "Remove you from {n} channel admin list",
        "Remove you from {n} channel admin lists",
    ),
    "agent_post_requesters": (
        "Forget that you requested {n} post an agent made for you; keep the posts",
        "Forget that you requested {n} posts agents made for you; keep the posts",
    ),
    "platform_user_names": ("Delete {n} remembered name", "Delete {n} remembered names"),
    "teams_messages": ("Delete {n} Teams message record", "Delete {n} Teams message records"),
}
_UNNUMBERED_CASCADE_ROWS = {
    "user_configs": "Remove your saved settings",  # Row count is storage detail.
    "account": "Remove your account",  # This is one account, never a count of accounts.
}
_CASCADE_EXCLUSIONS: dict[str, str] = {}  # Every purge category needs a deletion disclosure.

_COUNTED_SUMMARY_LABELS = {
    "linked_principals": ("linked account", "linked accounts"),
    "routines": ("routine", "routines"),
    "user_skills": ("skill you synced", "skills you synced"),
    "github_connect_requests": ("GitHub setup request", "GitHub setup requests"),
    "github_connect_click_intents": ("GitHub connect button", "GitHub connect buttons"),
    "github_user_links": ("linked GitHub account", "linked GitHub accounts"),
    "mcp_tokens": ("Daimon access token", "Daimon access tokens"),
    "gemini_records": ("Gemini conversation record", "Gemini conversation records"),
    "slack_user_tokens": ("saved Slack access token", "saved Slack access tokens"),
    "slack_turn_contexts": ("temporary Slack request record", "temporary Slack request records"),
    "direct_message_conversations": ("private conversation", "private conversations"),
    "credential_requests": ("credential request", "credential requests"),
    "wizard_sessions": ("wizard session", "wizard sessions"),
    "message_feedback": ("message feedback record", "message feedback records"),
    "support_escalations": ("support escalation", "support escalations"),
    "channel_admins": ("channel admin list", "channel admin lists"),
    "agent_post_requesters": ("post an agent made for you", "posts agents made for you"),
    "platform_user_names": ("remembered name", "remembered names"),
    "teams_messages": ("Teams message record", "Teams message records"),
}
_UNNUMBERED_SUMMARY_LABELS = {"user_configs": "saved settings"}
_SUMMARY_EXCLUSIONS = {
    "account": "The account itself is structural bookkeeping, disclosed in the deletion preview.",
    "principal_links": "Links between principals are bookkeeping, disclosed in the deletion preview.",
}


@pytest.fixture
def empty_preview() -> PurgePreview:
    return PurgePreview(
        **{name: PurgePreviewRow(count=0, example=None) for name in PurgePreview.model_fields}
    )


def _one_field(preview: PurgePreview, field: str, count: int) -> PurgePreview:
    return preview.model_copy(
        update={field: PurgePreviewRow(count=count, example="synthetic-example")}
    )


def _slack_delete(preview: PurgePreview) -> dict[str, Any]:
    return build_delete_modal(
        preview, account_id=_ACCOUNT_ID, user_name=_NAME, view_id="synthetic-view"
    )


def _slack_text(view: dict[str, Any]) -> str:
    return "\n".join(
        block["text"]["text"] for block in view["blocks"] if block["type"] == "section"
    )


def _teams_delete(preview: PurgePreview) -> AdaptiveCard:
    return confirm_card(preview, account_id=_ACCOUNT_ID, name=_NAME)


def _teams_text(card: AdaptiveCard) -> str:
    return "\n".join(item.text for item in card.body or [] if isinstance(item, TextBlock))


def _discord_text(preview: PurgePreview) -> str:
    return "\n".join(
        item.content
        for item in build_cascade_preview_container(preview).children
        if isinstance(item, discord.ui.TextDisplay)
    )


_CASCADE_RENDERERS: dict[str, Callable[[PurgePreview], str]] = {
    "discord": _discord_text,
    "slack": lambda preview: _slack_text(_slack_delete(preview)),
    "teams": lambda preview: _teams_text(_teams_delete(preview)),
}
_SUMMARY_RENDERERS: dict[str, Callable[[PurgePreview], str]] = {
    "shared": summary_line,
    "slack": lambda preview: _slack_text(build_export_result_view(summary=summary_line(preview))),
    "teams": lambda preview: _teams_text(export_card(preview, bot="daimon")),
}
# Discord intentionally has no export summary: its Export control is a placeholder.


def _plain(text: str) -> str:
    return text.translate(str.maketrans("", "", "*`_"))


def _cascade_rows(platform: str, preview: PurgePreview) -> list[str]:
    text = _plain(_CASCADE_RENDERERS[platform](preview))
    _, marker, body = text.partition("What will happen")
    assert marker, "the deletion explanation must have a visible section heading"
    body, marker, _ = body.partition("📋 What is intentionally kept elsewhere")
    assert marker, "the deletion explanation must retain the kept-data disclosure"
    return [line.strip() for line in body.splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("counted", "unnumbered", "excluded"),
    [
        (_COUNTED_CASCADE_ROWS, _UNNUMBERED_CASCADE_ROWS, _CASCADE_EXCLUSIONS),
        (_COUNTED_SUMMARY_LABELS, _UNNUMBERED_SUMMARY_LABELS, _SUMMARY_EXCLUSIONS),
    ],
    ids=["deletion-previews", "export-summaries"],
)
def test_every_preview_field_has_an_explicit_rendering_decision(
    counted: dict[str, tuple[str, str]], unnumbered: dict[str, str], excluded: dict[str, str]
) -> None:
    assert not (
        counted.keys() & unnumbered.keys()
        | counted.keys() & excluded.keys()
        | unnumbered.keys() & excluded.keys()
    )
    covered = counted.keys() | unnumbered.keys() | excluded.keys()
    assert covered == PurgePreview.model_fields.keys(), (
        f"Add a rendered expectation or justified exclusion for new fields: "
        f"{PurgePreview.model_fields.keys() - covered}; remove stale fields: "
        f"{covered - PurgePreview.model_fields.keys()}"
    )
    assert all(excluded.values()), "intentional exclusions must explain why"


@pytest.mark.parametrize("platform", _CASCADE_RENDERERS)
@pytest.mark.parametrize("field", _COUNTED_CASCADE_ROWS)
@pytest.mark.parametrize("count", [1, 2])
def test_each_nonzero_category_has_its_count_and_meaning_in_each_deletion_preview(
    platform: str, field: str, count: int, empty_preview: PurgePreview
) -> None:
    rows = _cascade_rows(platform, _one_field(empty_preview, field, count))
    expected = _COUNTED_CASCADE_ROWS[field][count - 1].format(n=count)
    assert len(rows) == 1, "only the one nonzero category should be listed"
    assert rows[0].split(", for example", 1)[0].endswith(expected)
    assert "Nothing to delete" not in rows[0]


@pytest.mark.parametrize("platform", _CASCADE_RENDERERS)
@pytest.mark.parametrize(
    ("field", "count"), [("account", 1), ("user_configs", 1), ("user_configs", 2)]
)
def test_account_and_settings_keep_their_unnumbered_deletion_copy(
    platform: str, field: str, count: int, empty_preview: PurgePreview
) -> None:
    rows = _cascade_rows(platform, _one_field(empty_preview, field, count))
    assert len(rows) == 1
    assert _UNNUMBERED_CASCADE_ROWS[field] in rows[0]
    assert not any(character.isdigit() for character in rows[0])


@pytest.mark.parametrize("platform", _CASCADE_RENDERERS)
def test_all_zero_categories_render_only_the_empty_deletion_message(
    platform: str, empty_preview: PurgePreview
) -> None:
    rows = _cascade_rows(platform, empty_preview)
    assert len(rows) == 1
    assert "Nothing to delete." in rows[0]


@pytest.mark.parametrize("surface", _SUMMARY_RENDERERS)
@pytest.mark.parametrize("field", _COUNTED_SUMMARY_LABELS)
@pytest.mark.parametrize("count", [1, 2])
def test_each_nonzero_category_has_its_count_and_meaning_in_export_summaries(
    surface: str, field: str, count: int, empty_preview: PurgePreview
) -> None:
    preview = _one_field(empty_preview, field, count)
    expected = f"{count} {_COUNTED_SUMMARY_LABELS[field][count - 1]}"
    assert summary_line(preview) == expected
    assert expected in _SUMMARY_RENDERERS[surface](preview)


@pytest.mark.parametrize("surface", _SUMMARY_RENDERERS)
@pytest.mark.parametrize("count", [1, 2])
def test_summary_settings_remain_unnumbered(
    surface: str, count: int, empty_preview: PurgePreview
) -> None:
    preview = _one_field(empty_preview, "user_configs", count)
    assert summary_line(preview) == "saved settings"
    assert "saved settings" in _SUMMARY_RENDERERS[surface](preview)


@pytest.mark.parametrize("surface", _SUMMARY_RENDERERS)
@pytest.mark.parametrize("field", [None, *_SUMMARY_EXCLUSIONS])
def test_zero_or_intentionally_excluded_summary_categories_are_not_counted(
    surface: str, field: str | None, empty_preview: PurgePreview
) -> None:
    preview = empty_preview if field is None else _one_field(empty_preview, field, 1)
    assert summary_line(preview) == "nothing listed here"
    assert "nothing listed here" in _SUMMARY_RENDERERS[surface](preview)


def test_discord_export_remains_an_explicit_placeholder() -> None:
    text = _export_placeholder_message("Synthetic bot")
    assert "Export** is not yet implemented" in text
    assert "every Synthetic bot-side row" in text
    assert "holds:" not in text


@pytest.fixture(params=["🧪" * 2_000, "&" * 2_000], ids=["long-unicode", "long-mrkdwn"])
def full_preview(request: pytest.FixtureRequest) -> PurgePreview:
    values = {
        name: PurgePreviewRow(count=9_999_999_999, example=request.param)
        for name in PurgePreview.model_fields
    }
    values["account"] = PurgePreviewRow(count=1, example=None)
    return PurgePreview(**values)


@pytest.mark.parametrize("platform", _CASCADE_RENDERERS)
def test_all_categories_survive_large_counts_and_long_examples(
    platform: str, full_preview: PurgePreview
) -> None:
    rows = _cascade_rows(platform, full_preview)
    text = "\n".join(rows)
    assert len(rows) == len(_COUNTED_CASCADE_ROWS) + len(_UNNUMBERED_CASCADE_ROWS)
    for _, plural in _COUNTED_CASCADE_ROWS.values():
        assert plural.format(n=9_999_999_999) in text
    for expected in _UNNUMBERED_CASCADE_ROWS.values():
        assert expected in text


@pytest.mark.parametrize("count", [1, 2])
def test_summary_preserves_each_count_when_fields_share_one_row(count: int) -> None:
    # Distinct schema fields may share one immutable value. Only user_configs
    # should become "saved settings", even when all their row objects alias it.
    values = dict.fromkeys(PurgePreview.model_fields, PurgePreviewRow(count=count, example=None))
    values["account"] = PurgePreviewRow(count=1, example=None)
    parts = summary_line(PurgePreview(**values)).split(", ")
    assert len(parts) == len(_COUNTED_SUMMARY_LABELS) + len(_UNNUMBERED_SUMMARY_LABELS)
    for labels in _COUNTED_SUMMARY_LABELS.values():
        assert f"{count} {labels[count - 1]}" in parts
    assert parts.count("saved settings") == 1


@pytest.mark.parametrize("platform", _CASCADE_RENDERERS)
@pytest.mark.parametrize("field", ["linked_principals", "user_skills"])
@pytest.mark.parametrize(
    ("example", "expected"),
    [(None, "—"), ("", "—"), ("x" * 100, "x" * 100), ("x" * 101, "x" * 99 + "…")],
    ids=["missing", "empty", "at-limit", "past-limit"],
)
def test_examples_are_readable_and_bounded_without_losing_the_category(
    platform: str,
    field: str,
    example: str | None,
    expected: str,
    empty_preview: PurgePreview,
) -> None:
    preview = empty_preview.model_copy(update={field: PurgePreviewRow(count=1, example=example)})
    rows = _cascade_rows(platform, preview)
    assert len(rows) == 1
    assert rows[0].endswith(expected)
    assert _COUNTED_CASCADE_ROWS[field][0].format(n=1) in rows[0]
    if example and len(example) > 100:
        assert example not in rows[0]


def test_full_discord_confirmation_fits_message_limits(full_preview: PurgePreview) -> None:
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.settings = MagicMock()
    runtime.settings.discord.bot_display_name = "Daimon"
    view = CascadePreviewView(
        runtime=runtime,
        account_id=_ACCOUNT_ID,
        allowed_user_id=123,
        user_name=_NAME,
        preview=full_preview,
    )
    children = list(view.walk_children())
    text_length = sum(
        len(item.content) for item in children if isinstance(item, discord.ui.TextDisplay)
    )
    assert text_length <= 4_000
    assert len(children) <= 40


def test_full_slack_confirmation_and_export_fit_modal_limits(full_preview: PurgePreview) -> None:
    for view in (
        _slack_delete(full_preview),
        build_export_result_view(summary=summary_line(full_preview)),
    ):
        assert len(view["blocks"]) <= 100
        for block in view["blocks"]:
            if block["type"] == "section":
                assert len(block["text"]["text"]) <= 3_000


def test_slack_splits_full_rows_before_reaching_its_section_limit(
    full_preview: PurgePreview, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tighter budget exercises splitting without depending on current copy
    # happening to fill Slack's production budget exactly.
    monkeypatch.setattr(slack_views, "MAX_SECTION_TEXT_CHARS", 600)
    view = _slack_delete(full_preview)
    sections = [block["text"]["text"] for block in view["blocks"] if block["type"] == "section"]
    assert len(sections) > 3, "the cascade should span multiple sections"
    assert all(len(text) <= 600 for text in sections)
    rows = _cascade_rows("slack", full_preview)
    assert len(rows) == len(_COUNTED_CASCADE_ROWS) + len(_UNNUMBERED_CASCADE_ROWS)
    for _, plural in _COUNTED_CASCADE_ROWS.values():
        expected = plural.format(n=9_999_999_999)
        assert sum(expected in row for row in rows) == 1, "a category must not be split or dropped"


def test_full_teams_confirmation_and_export_fit_activity_limit(full_preview: PurgePreview) -> None:
    for card in (_teams_delete(full_preview), export_card(full_preview, bot="Daimon")):
        activity = MessageActivityInput().add_card(card)
        activity.from_ = Account(id=f"28:{_ACCOUNT_ID}", name="Synthetic bot")
        activity.conversation = ConversationAccount(
            id=f"19:{'c' * 32}@thread.tacv2;messageid={'1' * 13}",
            tenant_id=str(uuid.UUID(int=2)),
            conversation_type="channel",
        )
        encoded = json.dumps(
            activity.model_dump(by_alias=True, exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        assert len(encoded) < 26_000

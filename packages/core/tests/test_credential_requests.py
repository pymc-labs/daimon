"""Unit tests for the credential-request wire contract (no I/O, no DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import get_args

from daimon.core.credential_requests import (
    CUSTOM_ID_PATTERN,
    ENV_FILE_TARGET,
    MAX_BUTTON_LABEL_CHARS,
    CredentialRequestKind,
    availability_for_request,
    build_button_label,
    build_custom_id,
    build_skill_repo_target,
    mint_request_token,
    split_skill_repo_target,
)
from daimon.core.stores.domain import CredentialRequestRow

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _credential_request_row(*, requested_work: str | None) -> CredentialRequestRow:
    """A consumed request row, as the store hands it back."""
    return CredentialRequestRow(
        token="tok-1",
        kind="env",
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        target="MY_KEY",
        mcp_server_url=None,
        requester_platform_user_id="discord-user-1",
        channel_id="C_FALLBACK",
        idempotency_key=uuid.uuid4(),
        requested_work=requested_work,
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        used_at=_NOW,
    )


def test_mint_request_token_returns_fresh_string_each_call() -> None:
    first = mint_request_token()
    second = mint_request_token()
    assert first != second, "two mints must never collide"


def test_build_custom_id_prefixes_the_token() -> None:
    token = mint_request_token()
    assert build_custom_id(token) == f"ztc:{token}"


def test_build_custom_id_stays_within_discord_custom_id_limit() -> None:
    custom_id = build_custom_id(mint_request_token())
    assert len(custom_id) <= 100, "custom_id must fit Discord's 100-char cap"


def test_custom_id_pattern_fullmatches_a_minted_custom_id_and_round_trips_the_token() -> None:
    token = mint_request_token()
    custom_id = build_custom_id(token)

    match = CUSTOM_ID_PATTERN.fullmatch(custom_id)

    assert match is not None, "the template must fullmatch a real minted custom_id"
    assert match["token"] == token, "the token group must round-trip the minted token"


def test_custom_id_pattern_rejects_bare_prefix() -> None:
    assert CUSTOM_ID_PATTERN.fullmatch("ztc:") is None, "a token-less custom_id must not match"


def test_custom_id_pattern_rejects_token_with_disallowed_characters() -> None:
    assert CUSTOM_ID_PATTERN.fullmatch("ztc:abc def") is None, (
        "a space is outside the allowed token charset"
    )


def test_custom_id_pattern_rejects_wrong_prefix() -> None:
    token = mint_request_token()
    assert CUSTOM_ID_PATTERN.fullmatch(f"xyz:{token}") is None, (
        "a custom_id minted by an unrelated feature must not match this template"
    )


def test_build_button_label_env() -> None:
    assert build_button_label("env", "OPENAI_API_KEY") == "Add key: OPENAI_API_KEY"


def test_build_button_label_mcp() -> None:
    assert build_button_label("mcp", "linear") == "Add MCP token: linear"


def test_build_button_label_truncates_long_target_within_discord_label_limit() -> None:
    label = build_button_label("mcp", "x" * 200)

    assert len(label) <= MAX_BUTTON_LABEL_CHARS, "label must fit Discord's 80-char button limit"
    assert label.startswith("Add MCP token: "), "truncation must not lose the human-readable prefix"


def test_build_button_label_repo() -> None:
    assert (
        build_button_label("repo", "clsandoval/daimon-qa-scratch")
        == "Set working repo: clsandoval/daimon-qa-scratch"
    )


def test_build_button_label_truncates_long_repo_target_within_discord_label_limit() -> None:
    label = build_button_label("repo", "https://github.com/" + "x" * 200)

    assert len(label) <= MAX_BUTTON_LABEL_CHARS, "label must fit Discord's 80-char button limit"
    assert label.startswith("Set working repo: "), (
        "truncation must not lose the human-readable prefix"
    )
    assert label.endswith("…"), "truncation must append the single-character ellipsis"


def test_build_button_label_skill_repo_does_not_say_bind() -> None:
    label = build_button_label("skill_repo", "clsandoval/seedance-2.0")

    assert label == "Import skills from: clsandoval/seedance-2.0"
    assert "Bind" not in label, (
        "a skill import writes no agent_repo_binding, so the label must not "
        "promise the user a checkout they did not ask for"
    )


def test_skill_repo_target_round_trips_url_branch_and_path() -> None:
    for url, branch, path in [
        ("https://github.com/o/r", "main", ""),
        ("https://github.com/o/r", "dev", "skills"),
        ("https://github.com/o/r", "release/1.x", "nested/skills"),
    ]:
        packed = build_skill_repo_target(url, branch, path)

        assert split_skill_repo_target(packed) == (url, branch, path)


def test_skill_repo_target_splits_path_before_branch() -> None:
    """A path may contain '@'; splitting on '@' first would mangle it."""
    packed = build_skill_repo_target("https://github.com/o/r", "main", "a@b/c")

    assert split_skill_repo_target(packed) == ("https://github.com/o/r", "main", "a@b/c")


def test_split_skill_repo_target_defaults_match_sync_skills_defaults() -> None:
    """A bare url must resolve to sync_skills' own defaults, not to empties."""
    assert split_skill_repo_target("https://github.com/o/r") == (
        "https://github.com/o/r",
        "main",
        "",
    )


def test_slack_action_id_is_stable() -> None:
    from daimon.core.credential_requests import SLACK_ACTION_ID

    assert SLACK_ACTION_ID == "credential_request"


def test_build_button_label_honours_slack_label_limit() -> None:
    from daimon.core.credential_requests import MAX_SLACK_BUTTON_LABEL_CHARS

    label = build_button_label("mcp", "x" * 200, max_chars=MAX_SLACK_BUTTON_LABEL_CHARS)
    assert len(label) == MAX_SLACK_BUTTON_LABEL_CHARS == 75
    assert label.startswith("Add MCP token: ")
    assert label.endswith("…")


def test_build_button_label_default_limit_unchanged() -> None:
    label = build_button_label("mcp", "x" * 200)
    assert len(label) == MAX_BUTTON_LABEL_CHARS == 80


def test_build_button_label_covers_every_kind() -> None:
    """Every declared kind must have a label prefix — a missing one is a KeyError at click time."""
    kinds = get_args(CredentialRequestKind)
    assert kinds, "CredentialRequestKind must declare at least one kind"
    for kind in kinds:
        label = build_button_label(kind, "acme/skills")
        assert label.endswith("acme/skills"), f"kind {kind} must name its target in the label"
        assert label != "acme/skills", f"kind {kind} must carry a prefix explaining the action"


def test_env_file_label_reads_as_a_file_import() -> None:
    assert build_button_label("env_file", ENV_FILE_TARGET) == "Add keys from .env", (
        "the env_file button must say it imports a whole file, not a single key"
    )


def test_availability_for_request_returns_saved_when_no_requested_work() -> None:
    row = _credential_request_row(requested_work=None)
    assert availability_for_request(row) == "saved", (
        "a request with nothing waiting on it is a plain save"
    )


def test_availability_for_request_returns_next_message_when_requested_work() -> None:
    row = _credential_request_row(requested_work="finish the churn writeup")
    assert availability_for_request(row) == "next_message", (
        "a request minted mid-task owes a turn off the person's next message"
    )

"""Layout of the five private forms a posted control's button opens.

These tests are about shape, not behaviour: how many components Discord is
asked to render, that the one input is wrapped in a `Label` (the 2.6+ way to
label an input) rather than carrying a deprecated `label=` of its own, and
that the title fits the platform's 45-character cap however long the key
name, repo or agent name turns out to be.

No DB and no runtime: every form's `__init__` reads the request row and
stores the runtime without touching either, so a `MagicMock` runtime is the
honest stand-in here and the DB fixtures would only make the file slow.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.discord.credential_modals import (
    EnvCredentialModal,
    EnvFileModal,
    McpCredentialModal,
    RepoBindModal,
    SkillRepoModal,
)
from daimon.core.credential_requests import ENV_FILE_TARGET, build_skill_repo_target
from daimon.core.stores.domain import CredentialRequestRow

# Discord's own caps, restated here so a change to the production constants
# has to be made deliberately rather than inherited by the assertions.
_MAX_TITLE_CHARS = 45
_MAX_LABEL_TEXT_CHARS = 45
_MAX_LABEL_DESCRIPTION_CHARS = 100

_SKILL_REPO_TARGET = build_skill_repo_target("https://github.com/acme/skills", "main", "")
_REPO_TARGET = build_skill_repo_target("https://github.com/acme/analytics", "main", "")


def _row(
    *,
    kind: str,
    target: str,
    target_name: str | None = "research-bot",
    mcp_server_url: str | None = None,
) -> CredentialRequestRow:
    now = datetime.now(UTC)
    return CredentialRequestRow(
        idempotency_key=uuid.uuid4(),
        token="layouttoken1234567890",
        kind=kind,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        target=target,
        mcp_server_url=mcp_server_url,
        requester_platform_user_id="100000000000000001",
        channel_id="chan-1",
        target_name=target_name,
        responder_name="Daimon",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        used_at=None,
    )


def _build(kind: str, *, target_name: str | None = "research-bot") -> discord.ui.Modal:
    """The form the button opens for `kind`, built the way `callback` builds it."""
    runtime = MagicMock()
    if kind == "env":
        return EnvCredentialModal(
            runtime=runtime,
            request_row=_row(kind="env", target="OPENAI_API_KEY", target_name=target_name),
        )
    if kind == "env_file":
        return EnvFileModal(
            runtime=runtime,
            request_row=_row(kind="env_file", target=ENV_FILE_TARGET, target_name=target_name),
        )
    if kind == "mcp":
        return McpCredentialModal(
            runtime=runtime,
            request_row=_row(
                kind="mcp",
                target="linear",
                target_name=target_name,
                mcp_server_url="https://mcp.linear.app/sse",
            ),
        )
    if kind == "skill_repo":
        return SkillRepoModal(
            runtime=runtime,
            request_row=_row(kind="skill_repo", target=_SKILL_REPO_TARGET, target_name=target_name),
        )
    return RepoBindModal(
        runtime=runtime,
        request_row=_row(kind="repo", target=_REPO_TARGET, target_name=target_name),
    )


_KINDS = ["env", "env_file", "mcp", "skill_repo", "repo"]


@pytest.mark.parametrize("kind", _KINDS)
def test_every_form_is_one_note_above_one_labelled_input(kind: str) -> None:
    modal = _build(kind)

    children = modal.children
    assert len(children) == 2, (
        f"{kind} must ask for exactly one thing under one note — Discord counts a Label "
        f"as one child, so two children is the whole form"
    )
    assert isinstance(children[0], discord.ui.TextDisplay), (
        f"{kind} must open with the note restating what the card already said"
    )
    assert isinstance(children[1], discord.ui.Label), (
        f"{kind}'s input must be wrapped in a Label, which is where its visible text lives"
    )


@pytest.mark.parametrize("kind", _KINDS)
def test_every_form_label_fits_discords_caps(kind: str) -> None:
    label = _build(kind).children[1]
    assert isinstance(label, discord.ui.Label), f"{kind}'s second child must be the Label"

    assert len(label.text) <= _MAX_LABEL_TEXT_CHARS, (
        f"{kind}: Discord rejects a Label longer than {_MAX_LABEL_TEXT_CHARS} codepoints"
    )
    if label.description is not None:
        assert len(label.description) <= _MAX_LABEL_DESCRIPTION_CHARS, (
            f"{kind}: Discord rejects a description longer than "
            f"{_MAX_LABEL_DESCRIPTION_CHARS} codepoints"
        )


@pytest.mark.parametrize("kind", _KINDS)
def test_every_form_note_is_small_text(kind: str) -> None:
    note = _build(kind).children[0]
    assert isinstance(note, discord.ui.TextDisplay), f"{kind}'s first child must be the note"

    assert note.content.startswith("-# "), (
        f"{kind}'s note restates facts the card already carried, so it is subordinate to the "
        f"input and is rendered as small text"
    )


@pytest.mark.parametrize("kind", _KINDS)
def test_no_form_asks_for_a_branch(kind: str) -> None:
    modal = _build(kind)

    rendered = " ".join(
        item.text if isinstance(item, discord.ui.Label) else "" for item in modal.children
    )
    assert "branch" not in rendered.lower(), (
        f"{kind}: the branch is fixed by the request row and must never be retyped"
    )


@pytest.mark.parametrize("kind", _KINDS)
def test_every_form_title_fits_discords_cap(kind: str) -> None:
    for target_name in ("research-bot", "b" * 80, None):
        title = _build(kind, target_name=target_name).title
        assert len(title) <= _MAX_TITLE_CHARS, (
            f"{kind}: Discord rejects a form title longer than {_MAX_TITLE_CHARS} "
            f"characters (agent name {target_name!r})"
        )


def test_title_names_both_the_target_and_the_agent_when_they_fit() -> None:
    modal = _build("env")

    assert modal.title == "OPENAI_API_KEY for research-bot", (
        "a title with room for both should say which key is going to which agent"
    )


def test_title_drops_the_agent_when_the_pair_would_overflow() -> None:
    modal = _build("env", target_name="a-very-long-agent-name-that-keeps-going")

    assert modal.title == "OPENAI_API_KEY", (
        "a long agent name costs the agent, not the key the person came to enter"
    )


def test_title_falls_back_to_the_generic_name_when_the_target_alone_overflows() -> None:
    runtime = MagicMock()
    modal = EnvCredentialModal(runtime=runtime, request_row=_row(kind="env", target="K" * 50))

    assert modal.title == "Add a key", (
        "a target too long to show at all leaves only the generic name for this form"
    )


def test_env_file_form_takes_exactly_one_file() -> None:
    modal = _build("env_file")
    label = modal.children[1]
    assert isinstance(label, discord.ui.Label), "the env_file form's input must be Label-wrapped"

    upload: Any = label.component
    assert isinstance(upload, discord.ui.FileUpload), (
        "the env_file form asks for a file, not typed text"
    )
    assert upload.max_values == 1, "one upload is one .env file, never a batch of them"
    assert upload.min_values == 1, "the form cannot be submitted with nothing attached"
    assert upload.required is True, "the file is the whole point of this form"


@pytest.mark.parametrize("kind", ["env", "mcp", "skill_repo", "repo"])
def test_typed_forms_wrap_a_text_input(kind: str) -> None:
    label = _build(kind).children[1]
    assert isinstance(label, discord.ui.Label), f"{kind}'s input must be Label-wrapped"

    assert isinstance(label.component, discord.ui.TextInput), (
        f"{kind} collects one typed value through the Label's component"
    )

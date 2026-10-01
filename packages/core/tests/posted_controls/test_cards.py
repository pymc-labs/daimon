"""The posted-card model: the full kind x state matrix, its copy, and its guards."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from daimon.core.continuity.messages import FORBIDDEN_TOKENS, ConfigurationChange
from daimon.core.credential_requests import build_custom_id, build_skill_repo_target
from daimon.core.posted_controls.cards import (
    ALREADY_USED_MESSAGE,
    EXPIRED_HEADLINE,
    FOOTER_TEMPLATE,
    NO_LONGER_VALID_MESSAGE,
    RECEIVED_FOOTER,
    REPLACED_HEADLINE,
    WRONG_REQUESTER_MESSAGE,
    CardButton,
    CardKind,
    CardState,
    PostedCard,
    build_posted_card,
    card_for_request,
    card_text,
    classify_card_state,
    expired_message,
)
from daimon.core.stores.domain import CredentialRequestRow

AGENT = "research-bot"
RESPONDER = "Daimon"
REQUESTER = "U04ABCDEF"
EXPIRES = datetime(2026, 9, 14, 17, 30, tzinfo=UTC)
TOKEN = "vJ8xQ2mZ1pKd7nRb3sTyLw"
MCP_URL = "https://mcp.linear.app/sse"

KINDS: tuple[CardKind, ...] = ("env", "env_file", "mcp", "mcp_oauth", "repo", "skill_repo")
STATES: tuple[CardState, ...] = (
    "requested",
    "received",
    "applied",
    "partial",
    "refused",
    "expired",
    "superseded",
    "replaced",
)

TARGETS: dict[CardKind, str] = {
    "env": "TOGGL_TOKEN",
    "env_file": ".env",
    "mcp": "Linear",
    "mcp_oauth": "Notion",
    "repo": "acme/analytics",
    "skill_repo": "acme/skills",
}

# One confirmation per kind, for the states that carry an outcome. Each uses a
# ChangeKind `render_change_confirmation` accepts today.
OUTCOMES: dict[CardKind, ConfigurationChange] = {
    "env": ConfigurationChange(
        target_name=AGENT, kind="key", availability="saved", detail="TOGGL_TOKEN"
    ),
    "env_file": ConfigurationChange(
        target_name=AGENT, kind="keys_bulk", availability="saved", count=3
    ),
    "mcp": ConfigurationChange(
        target_name=AGENT, kind="mcp", availability="next_message", detail="Linear"
    ),
    "mcp_oauth": ConfigurationChange(
        target_name=AGENT, kind="mcp", availability="next_message", detail="Notion"
    ),
    "repo": ConfigurationChange(
        target_name=AGENT,
        kind="repo",
        availability="saved",
        repo="acme/analytics",
        branch="main",
    ),
    "skill_repo": ConfigurationChange(
        target_name=AGENT, kind="skill", availability="next_message", detail="churn-report"
    ),
}

# `received` repeats the requested headline and `superseded` shares partial's
# warning sign, so those two read back as their emoji twin.
CLASSIFIED_AS: dict[CardState, CardState] = {
    "requested": "requested",
    "received": "requested",
    "applied": "applied",
    "partial": "partial",
    "refused": "refused",
    "expired": "expired",
    "superseded": "partial",
    "replaced": "replaced",
}


def build(kind: CardKind, state: CardState, **overrides: Any) -> PostedCard:
    """Build one card of the matrix, filling every per-state requirement."""
    kwargs: dict[str, Any] = {
        "kind": kind,
        "state": state,
        "agent_name": AGENT,
        "responder_name": RESPONDER,
        "target": TARGETS[kind],
        "requester_platform_user_id": REQUESTER,
        "expires_at": EXPIRES,
        "token": TOKEN,
        "branch": "main",
        "mcp_server_url": MCP_URL,
        "skill_repo_isolated": True,
    }
    if state in ("applied", "partial"):
        kwargs["outcome"] = OUTCOMES[kind]
    if state == "refused":
        kwargs["refusal"] = "admin_required"
    kwargs.update(overrides)
    return build_posted_card(**kwargs)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", STATES)
def test_build_posted_card_builds_every_kind_in_every_state(
    kind: CardKind, state: CardState
) -> None:
    card = build(kind, state)

    assert card.kind == kind, "the card should keep the kind it was built for"
    assert card.state == state, "the card should keep the state it was built for"
    assert card.headline, f"{kind}/{state} must have a headline"
    assert not card.headline.endswith("\n"), "a headline is one line, never newline-terminated"
    assert all(fact for fact in card.facts), f"{kind}/{state} must not carry a blank fact"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", STATES)
def test_only_requested_carries_buttons_requester_and_expiry(
    kind: CardKind, state: CardState
) -> None:
    card = build(kind, state)

    if state == "requested":
        assert len(card.buttons) == 1, "a requested card offers exactly the private-form button"
        assert card.buttons[0].custom_id == build_custom_id(TOKEN), (
            "the button must carry the request token as its custom_id"
        )
        assert card.buttons[0].style == "primary", "the private-form button is the primary action"
        assert card.footer == FOOTER_TEMPLATE, (
            "the requested footer is the template the renderers substitute into"
        )
        assert card.requester_platform_user_id == REQUESTER, "the requester must reach the renderer"
        assert card.expires_at_unix == int(EXPIRES.timestamp()), (
            "the expiry must reach the renderer as a unix timestamp"
        )
        return

    assert card.buttons == (), f"{state} must offer no buttons"
    assert card.requester_platform_user_id is None, f"{state} must not name a requester"
    assert card.expires_at_unix is None, f"{state} must not carry an expiry"
    expected_footer = RECEIVED_FOOTER if state == "received" else None
    assert card.footer == expected_footer, f"{state} footer should be {expected_footer!r}"


def test_requested_copy_names_the_key_and_who_can_use_it() -> None:
    card = build("env", "requested")

    assert card.headline == f"🔑 Add TOGGL_TOKEN to {AGENT}", "the headline names key and agent"
    assert card.facts == (
        f"Anyone who talks to {AGENT} can use it.",
        "The value is not shown in chat.",
    ), "the env card states shared ownership and that the value stays out of chat"
    assert card.buttons[0].label == "🔐 Enter it privately", "env collects one value privately"


def test_requested_env_file_copy_describes_the_upload() -> None:
    card = build("env_file", "requested")

    assert card.headline == f"🔑 Add keys to {AGENT} from a .env file", (
        "the bulk headline names the file, not a single key"
    )
    assert card.facts == (
        "One KEY=VALUE per line.",
        "Daimon stores the keys, not a retained copy of your uploaded file.",
        f"Anyone who talks to {AGENT} can use them.",
    ), "the upload card explains the format and what is kept"
    assert card.buttons[0].label == "🔐 Upload it privately", "env_file takes a file, not a value"


def test_requested_mcp_copy_names_the_server_and_its_url() -> None:
    card = build("mcp", "requested")

    assert card.headline == f"🔌 Connect {AGENT} to Linear", "the headline names the service"
    assert card.facts == (
        f"{MCP_URL} needs a token.",
        f"Anyone who talks to {AGENT} can use this connection.",
    ), "the mcp card shows the fixed endpoint and shared use"
    assert card.buttons[0].label == "🔐 Enter the token privately", "mcp collects a bearer token"


def test_requested_repo_copy_names_the_branch_and_whose_token_it_is() -> None:
    card = build("repo", "requested", branch="release")

    assert card.headline == f"📦 Give {AGENT} access to acme/analytics", (
        "the headline names the repo being opened up"
    )
    assert card.facts == (
        "Branch `release`.",
        f"This repo is private, so {AGENT} needs a way in.",
        f"The token stays yours; {AGENT} uses it whenever it needs GitHub.",
    ), "the repo card names the branch, the reason and the token's owner"
    assert card.buttons[0].label == "🔐 Use my GitHub token", (
        "this phase offers the token fallback only"
    )


def test_requested_skill_repo_copy_says_the_working_repo_is_untouched() -> None:
    card = build("skill_repo", "requested", skill_repo_isolated=True)

    assert card.headline == f"🧩 Add skills to {AGENT} from acme/skills", (
        "the headline names the skill source"
    )
    assert card.facts[0] == f"Skill repo only — {AGENT}'s working repo does not change.", (
        "an isolated skill import must say the working repo is left alone"
    )
    assert card.facts[1:] == (
        "Branch `main`.",
        f"The token stays yours; {AGENT} uses it whenever it needs GitHub.",
    ), "the rest of the skill_repo card matches the repo card's token copy"


def test_skill_repo_omits_the_working_repo_line_when_not_isolated() -> None:
    card = build("skill_repo", "requested", skill_repo_isolated=False)

    assert all("working repo" not in fact for fact in card.facts), (
        "a non-isolated skill import must not promise the working repo is unchanged"
    )
    assert card.facts == (
        "Branch `main`.",
        f"The token stays yours; {AGENT} uses it whenever it needs GitHub.",
    ), "dropping the isolation line leaves the branch and token facts"


@pytest.mark.parametrize("kind", KINDS)
def test_received_repeats_the_requested_lines_and_says_it_is_saving(kind: CardKind) -> None:
    requested = build(kind, "requested")
    received = build(kind, "received")

    assert received.headline == requested.headline, (
        "the card must not change its subject on receipt"
    )
    assert received.facts == requested.facts, "receipt changes the footer, not the facts"
    assert received.footer == "Received. Saving…", "the footer says the value arrived"


@pytest.mark.parametrize("kind", KINDS)
def test_applied_card_renders_the_change_confirmation_under_a_tick(kind: CardKind) -> None:
    from daimon.core.continuity.messages import render_change_confirmation

    card = build(kind, "applied")
    lines = render_change_confirmation(OUTCOMES[kind]).split("\n")

    assert card.headline == f"✅ {lines[0]}", (
        "applied marks the confirmation's first line with a tick"
    )
    assert card.facts == tuple(lines[1:]), "the remaining confirmation lines become the facts"


@pytest.mark.parametrize("kind", KINDS)
def test_partial_card_renders_the_same_confirmation_under_a_warning(kind: CardKind) -> None:
    card = build(kind, "partial")

    assert card.headline.startswith("⚠️ "), "partial success is marked, not celebrated"
    assert card_text(card) != "", "a partial card still explains what did happen"


@pytest.mark.parametrize("state", ["applied", "partial"])
def test_outcome_that_claims_ready_now_is_refused(state: CardState) -> None:
    ready_now = ConfigurationChange(
        target_name=AGENT, kind="key", availability="ready_now", detail="TOGGL_TOKEN"
    )

    with pytest.raises(ValueError, match="ready_now"):
        build("env", state, outcome=ready_now)


@pytest.mark.parametrize("kind", KINDS)
def test_expired_card_says_what_to_ask_for_again(kind: CardKind) -> None:
    card = build(kind, "expired")

    assert card.headline == EXPIRED_HEADLINE, "every expired card opens with the same line"
    assert len(card.facts) == 1, "an expired card is two lines: what happened, and the way back"
    assert card.facts[0].startswith(f"Ask {RESPONDER} to "), (
        "the way back names the responder to ask"
    )
    assert card.facts[0].endswith(" again."), "the way back is a repeat of the original request"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("env", f"Ask {RESPONDER} to add TOGGL_TOKEN to {AGENT} again."),
        ("env_file", f"Ask {RESPONDER} to add keys to {AGENT} from a file again."),
        ("mcp", f"Ask {RESPONDER} to connect {AGENT} to Linear again."),
        ("mcp_oauth", f"Ask {RESPONDER} to connect {AGENT} to Notion again."),
        ("repo", f"Ask {RESPONDER} to give {AGENT} access to acme/analytics again."),
        ("skill_repo", f"Ask {RESPONDER} to add skills to {AGENT} from acme/skills again."),
    ],
)
def test_expired_fact_uses_the_kind_specific_phrase(kind: CardKind, expected: str) -> None:
    card = build(kind, "expired")

    assert card.facts[0] == expected, f"{kind} should describe its own request in the way back"


@pytest.mark.parametrize("kind", KINDS)
def test_expired_message_matches_the_expired_card_text(kind: CardKind) -> None:
    card = build(kind, "expired")
    message = expired_message(
        kind=kind, agent_name=AGENT, responder_name=RESPONDER, target=TARGETS[kind]
    )

    assert message == card_text(card), (
        "the ephemeral a late clicker gets must say what the card now says"
    )


def test_repo_overrides_the_target_for_display() -> None:
    card = build(
        "repo",
        "requested",
        target="https://github.com/acme/analytics.git",
        repo="acme/analytics",
    )

    assert card.headline == f"📦 Give {AGENT} access to acme/analytics", (
        "a row holding a clone URL should still show the short repo name"
    )


def test_admin_required_refusal_supplies_the_sentence_for_an_admin() -> None:
    card = build("repo", "refused", refusal="admin_required")

    assert card.headline == f"🛡️ {AGENT}'s working repo was not changed.", (
        "the refusal names what did not happen"
    )
    assert card.facts == (
        f"An admin can ask {RESPONDER} to give {AGENT} access to acme/analytics.",
        "Nothing was saved.",
    ), "the refusal hands over the exact sentence an admin can say"


def test_replacement_refusal_names_the_key_it_did_not_replace() -> None:
    card = build("env", "refused", refusal="replacement_admin_required")

    assert card.headline == f"🛡️ TOGGL_TOKEN was not replaced for {AGENT}.", (
        "the refusal names the key that stayed"
    )
    assert card.facts == (
        f"An admin can ask {RESPONDER} to replace TOGGL_TOKEN on {AGENT}.",
        "The existing key is unchanged.",
    ), "a refused replacement states the next step and that nothing changed"


def test_target_unavailable_refusal_says_nothing_was_saved_and_how_to_ask_again() -> None:
    card = build("mcp", "refused", refusal="target_unavailable")

    assert card.headline == f"🛡️ Nothing was saved for {AGENT}.", (
        "the person who filled in the form must not be left guessing what landed"
    )
    assert card.facts == (
        f"{AGENT} is not available right now. Ask {RESPONDER} again.",
        # No blame and no error string: the target went away, the submitter
        # did nothing wrong, and asking again is the whole remedy.
    ), "a target that is gone offers the one way forward there is"


def test_env_file_refusal_shows_the_supplied_reasons_verbatim() -> None:
    reasons = ("Line 4: no = sign.", "Line 9: key name has a space.")

    card = build("env_file", "refused", refusal="env_file_invalid", refusal_lines=reasons)

    assert card.headline == f"🛡️ No keys were saved for {AGENT}.", "a batch fails as a batch"
    assert card.facts == reasons, "the caller's line-numbered reasons are shown as given"


def test_superseded_card_says_someone_else_got_there_first() -> None:
    card = build("env", "superseded")

    assert card.headline == f"⚠️ TOGGL_TOKEN was not replaced for {AGENT}.", (
        "a stale form names the key it did not overwrite"
    )
    assert card.facts == (
        "Someone else changed it while this form was open.",
        "The current value is unchanged.",
        f"Ask {RESPONDER} to replace TOGGL_TOKEN on {AGENT} again.",
    ), "the stale-form card explains the race and offers the retry"


@pytest.mark.parametrize("kind", KINDS)
def test_replaced_card_points_at_the_newer_form_and_names_nothing_else(kind: CardKind) -> None:
    card = build(kind, "replaced")

    assert card.headline == REPLACED_HEADLINE, "every kind retires with the same one headline"
    assert card.facts == ("Use the newer form below.",), (
        "a retired form says only where the live one is"
    )
    assert TARGETS[kind] not in card_text(card), (
        "the newer card names the target; repeating it here is what confused the reader"
    )


def test_replaced_is_told_apart_from_expired_despite_sharing_an_emoji() -> None:
    replaced = build("env", "replaced")
    expired = build("env", "expired")

    assert replaced.headline[0] == expired.headline[0], "both retirements wear the hourglass"
    assert classify_card_state(replaced.headline) == "replaced", (
        "the replaced headline is one fixed string, so it classifies exactly"
    )
    assert classify_card_state(expired.headline) == "expired", (
        "the expired headline must not be dragged along by the shared emoji"
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", STATES)
def test_no_card_string_uses_a_forbidden_token(kind: CardKind, state: CardState) -> None:
    card = build(kind, state)
    strings = [card.headline, *card.facts, *(button.label for button in card.buttons)]
    if card.footer is not None:
        strings.append(card.footer)

    for text in strings:
        lowered = text.lower()
        for forbidden in FORBIDDEN_TOKENS:
            assert forbidden not in lowered, f"{kind}/{state} says {forbidden!r} in {text!r}"


@pytest.mark.parametrize(
    "message",
    [
        NO_LONGER_VALID_MESSAGE,
        WRONG_REQUESTER_MESSAGE,
        ALREADY_USED_MESSAGE,
        EXPIRED_HEADLINE,
        REPLACED_HEADLINE,
    ],
)
def test_shared_refusal_messages_avoid_forbidden_tokens(message: str) -> None:
    lowered = message.lower()

    for forbidden in FORBIDDEN_TOKENS:
        assert forbidden not in lowered, f"{message!r} says {forbidden!r}"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", STATES)
def test_classify_card_state_reads_the_state_back_off_the_headline(
    kind: CardKind, state: CardState
) -> None:
    card = build(kind, state)

    assert classify_card_state(card.headline) == CLASSIFIED_AS[state], (
        f"{kind}/{state} should be recognisable from its leading emoji"
    )


def test_classify_card_state_returns_none_for_a_line_with_no_state_emoji() -> None:
    assert classify_card_state("Just a sentence.") is None, (
        "an unmarked line belongs to no card state"
    )


def test_applied_without_an_outcome_is_refused() -> None:
    with pytest.raises(ValueError, match="outcome"):
        build_posted_card(
            kind="env",
            state="applied",
            agent_name=AGENT,
            responder_name=RESPONDER,
            target="TOGGL_TOKEN",
            requester_platform_user_id=REQUESTER,
            expires_at=EXPIRES,
            token=TOKEN,
        )


def test_outcome_outside_an_outcome_state_is_refused() -> None:
    with pytest.raises(ValueError, match="outcome"):
        build("env", "expired", outcome=OUTCOMES["env"])


def test_refused_without_a_reason_is_rejected() -> None:
    with pytest.raises(ValueError, match="refusal"):
        build("repo", "refused", refusal=None)


def test_refusal_outside_the_refused_state_is_rejected() -> None:
    with pytest.raises(ValueError, match="refusal"):
        build("repo", "expired", refusal="admin_required")


def test_refusal_lines_outside_the_env_file_refusal_are_rejected() -> None:
    with pytest.raises(ValueError, match="refusal_lines"):
        build("repo", "refused", refusal="admin_required", refusal_lines=("Line 1: nope.",))


def test_env_file_refusal_without_reasons_is_rejected() -> None:
    with pytest.raises(ValueError, match="refusal_lines"):
        build("env_file", "refused", refusal="env_file_invalid")


def test_mcp_request_without_its_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="mcp_server_url"):
        build("mcp", "requested", mcp_server_url=None)


@pytest.mark.parametrize("kind", ["repo", "skill_repo"])
def test_repo_request_without_a_branch_is_rejected(kind: CardKind) -> None:
    with pytest.raises(ValueError, match="branch"):
        build(kind, "requested", branch=None)


def test_naive_expiry_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build("env", "requested", expires_at=datetime(2026, 9, 14, 17, 30))


def test_a_button_outside_the_requested_state_is_rejected() -> None:
    button = CardButton(label="🔐 Enter it privately", style="primary", custom_id="ztc:abc")

    with pytest.raises(ValueError, match="buttons"):
        PostedCard(kind="env", state="expired", headline=EXPIRED_HEADLINE, buttons=(button,))


def test_a_primary_button_without_a_custom_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="custom_id"):
        CardButton(label="🔐 Enter it privately", style="primary")


def test_a_link_button_without_a_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="url"):
        CardButton(label="Connect GitHub", style="link", custom_id="ztc:abc")


def _request_row(kind: str, target: str, mcp_server_url: str | None = None) -> CredentialRequestRow:
    return CredentialRequestRow(
        token=TOKEN,
        kind=kind,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        target=target,
        mcp_server_url=mcp_server_url,
        requester_platform_user_id=REQUESTER,
        channel_id="C1",
        idempotency_key=uuid.uuid4(),
        target_name=AGENT,
        created_at=EXPIRES,
        expires_at=EXPIRES,
        used_at=None,
    )


def test_card_for_request_reads_the_row() -> None:
    """Every fact the card shows comes off the durable request row."""
    card = card_for_request(_request_row("mcp", "linear", MCP_URL), state="requested")
    assert card.headline == f"🔌 Connect {AGENT} to linear", "the row names agent and server"
    assert card.facts[0] == f"{MCP_URL} needs a token.", "the row carries the server url"


def test_card_for_request_unpacks_the_repo_target() -> None:
    """A repo row packs `url@branch#path` into `target`; the card shows owner/repo."""
    target = build_skill_repo_target("https://github.com/acme/skills", "dev", "skills")
    card = card_for_request(_request_row("skill_repo", target), state="requested")
    expected = build(
        "skill_repo",
        "requested",
        target=target,
        repo="acme/skills",
        branch="dev",
        mcp_server_url=None,
        skill_repo_isolated=False,
    )
    assert card == expected, "the row's packed target yields the same card as explicit facts"

"""Adaptive Card rendering of a posted card, for Teams."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.posted_controls.teams_card import (
    CREDENTIAL_DIALOG,
    build_adaptive_card,
    card_for_request,
)
from daimon.core.stores.domain import CredentialRequestRow

from .test_cards import AGENT, KINDS, STATES, TOKEN, build


def test_requested_card_opens_the_dialog_with_the_token_and_a_local_time_footer() -> None:
    card = build_adaptive_card(build("env", "requested"), token=TOKEN)
    body = card["body"]
    assert isinstance(body, list)
    headline, *facts, actions, footer = body
    assert headline["text"] == f"🔑 Add TOGGL_TOKEN to {AGENT}"
    assert len(facts) == 2, "each fact is its own line"
    (button,) = actions["actions"]
    assert button["type"] == "Action.Submit"
    assert button["data"] == {
        "msteams": {"type": "task/fetch"},
        "dialog_id": CREDENTIAL_DIALOG,
        "token": TOKEN,
    }, "the button opens the dialog and carries only the opaque request token"
    assert "{{TIME(2026-09-14T17:30:00Z)}}" in footer["text"], "Teams renders it in local time"
    assert "the person who asked" in footer["text"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("state", [s for s in STATES if s != "requested"])
def test_every_later_state_renders_without_a_form_button(kind: str, state: str) -> None:
    card = json.dumps(build_adaptive_card(build(kind, state)))  # type: ignore[arg-type]
    assert "task/fetch" not in card and TOKEN not in card, "a spent card offers no form"


def test_a_form_button_without_its_token_is_refused() -> None:
    with pytest.raises(ValueError, match="token"):
        build_adaptive_card(build("mcp", "requested"))


def test_card_for_request_reads_the_row() -> None:
    now = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)
    row = CredentialRequestRow(
        token=TOKEN,
        kind="mcp",
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        target="linear",
        mcp_server_url="https://mcp.linear.app/sse",
        requester_platform_user_id="u",
        channel_id="a:chat",
        idempotency_key=uuid.uuid4(),
        target_name=AGENT,
        created_at=now,
        expires_at=now,
        used_at=None,
    )
    card = card_for_request(row, state="requested")
    assert card.headline == f"🔌 Connect {AGENT} to linear"
    assert card.facts[0] == "https://mcp.linear.app/sse needs a token."

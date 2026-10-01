"""Tests for `edit_posted_card` — the bot-side re-render of a posted card.

Behavioral assertions:

  - Every lifecycle state edits the request's OWN message (the channel and ts
    recorded on the row), not the channel the submission arrived from.
  - `requested` is the only state that carries the button, and the button
    carries the row's token; every later state drops the actions block.
  - Each state announces itself with its own headline emoji, and `received`
    keeps the requested headline and facts rather than collapsing the card.
  - A row with no recorded card ts edits nothing.
  - A Slack API failure is swallowed and logged with the error code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
import yarl
from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.posted_controls import edit_posted_card
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_requests import build_skill_repo_target
from daimon.core.posted_controls import RECEIVED_FOOTER, CardState
from daimon.core.stores.domain import CredentialRequestRow
from slack_sdk.web.async_client import AsyncWebClient

_CHAT_UPDATE_URL = yarl.URL("https://slack.com/api/chat.update")
_TOKEN = "tok_posted_card"
_USER_ID = "U_REQUESTER"
_POSTED_TS = "1700000002.000200"

_OUTCOME = ConfigurationChange(
    target_name="specialist",
    kind="key",
    detail="OPENAI_API_KEY",
    availability="next_message",
)


def _row(
    *,
    kind: str = "env",
    target: str = "OPENAI_API_KEY",
    mcp_server_url: str | None = None,
    posted_message_id: str | None = _POSTED_TS,
    parent_channel_id: str | None = "C_ORIGIN",
    channel_id: str = "C_FALLBACK",
) -> CredentialRequestRow:
    now = datetime.now(UTC)
    return CredentialRequestRow(
        token=_TOKEN,
        kind=kind,
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        target=target,
        mcp_server_url=mcp_server_url,
        requester_platform_user_id=_USER_ID,
        channel_id=channel_id,
        platform="slack",
        parent_channel_id=parent_channel_id,
        origin_thread_id="1700000000.000001",
        posted_message_id=posted_message_id,
        idempotency_key=uuid.uuid4(),
        target_name="specialist",
        responder_name="Daimon",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        used_at=None,
    )


def _payload(mock: AioResponsesMock) -> dict[str, Any]:
    calls = mock.requests[("POST", _CHAT_UPDATE_URL)]  # pyright: ignore[reportUnknownMemberType]
    assert len(calls) == 1, "one edit per call"
    body: dict[str, Any] = calls[0].kwargs["json"]
    return body


def _actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in payload["blocks"] if b["type"] == "actions"]


async def test_requested_state_edits_the_rows_own_card_with_the_button(
    fake_slack_web_client: Any,
) -> None:
    await edit_posted_card(fake_slack_web_client.client, row=_row(), state="requested")

    payload = _payload(fake_slack_web_client.mock)
    assert (payload["channel"], payload["ts"]) == ("C_ORIGIN", _POSTED_TS), (
        "the edit targets the card identity recorded on the row"
    )
    actions = _actions(payload)
    assert len(actions) == 1, "the requested state is the only one with a button"
    assert actions[0]["elements"][0]["value"] == _TOKEN, (
        "the button carries the row's single-use token"
    )
    assert payload["text"] == payload["blocks"][0]["text"]["text"].strip("*"), (
        "the notification text is the card's headline"
    )


async def test_received_state_keeps_the_headline_and_drops_the_button(
    fake_slack_web_client: Any,
) -> None:
    await edit_posted_card(fake_slack_web_client.client, row=_row(), state="received")

    payload = _payload(fake_slack_web_client.mock)
    assert payload["text"] == "🔑 Add OPENAI_API_KEY to specialist", (
        "received repeats the requested headline rather than collapsing the card"
    )
    assert not _actions(payload), "a consumed request must not keep a live button"
    facts = [b for b in payload["blocks"] if b["type"] == "context"]
    assert any("Anyone who talks to specialist can use it." in str(b) for b in facts), (
        "received keeps the facts the requested card showed"
    )
    assert RECEIVED_FOOTER in str(payload["blocks"][-1]), "received ends on its own footer"


@pytest.mark.parametrize(
    ("state", "emoji"),
    [("applied", "✅"), ("partial", "⚠️"), ("expired", "⌛"), ("superseded", "⚠️")],
)
async def test_each_terminal_state_announces_itself_without_a_button(
    state: str, emoji: str, fake_slack_web_client: Any
) -> None:
    card_state: CardState = state  # pyright: ignore[reportAssignmentType]  # parametrized literal
    outcome = _OUTCOME if state in ("applied", "partial") else None
    await edit_posted_card(
        fake_slack_web_client.client, row=_row(), state=card_state, outcome=outcome
    )

    payload = _payload(fake_slack_web_client.mock)
    assert payload["text"].startswith(emoji), f"state={state} must lead with {emoji}"
    assert not _actions(payload), f"state={state} must not offer the form again"
    assert (payload["channel"], payload["ts"]) == ("C_ORIGIN", _POSTED_TS), (
        "every state edits the same card"
    )


async def test_refused_state_names_the_repo_from_the_packed_target(
    fake_slack_web_client: Any,
) -> None:
    row = _row(kind="repo", target=build_skill_repo_target("https://github.com/o/r", "dev", ""))

    await edit_posted_card(
        fake_slack_web_client.client, row=row, state="refused", refusal="admin_required"
    )

    payload = _payload(fake_slack_web_client.mock)
    assert payload["text"].startswith("🛡️"), "a refusal leads with the refusal marker"
    assert "o/r" in str(payload["blocks"]), (
        "the packed target is unpacked to owner/repo for display"
    )
    assert not _actions(payload), "a refused request must not keep a live button"


async def test_a_row_without_a_posted_card_edits_nothing(fake_slack_web_client: Any) -> None:
    await edit_posted_card(
        fake_slack_web_client.client, row=_row(posted_message_id=None), state="received"
    )

    assert ("POST", _CHAT_UPDATE_URL) not in fake_slack_web_client.mock.requests, (
        "there is no card to edit when the row never recorded one"
    )


async def test_the_card_falls_back_to_the_rows_channel_without_a_parent(
    fake_slack_web_client: Any,
) -> None:
    await edit_posted_card(
        fake_slack_web_client.client, row=_row(parent_channel_id=None), state="received"
    )

    assert _payload(fake_slack_web_client.mock)["channel"] == "C_FALLBACK", (
        "a row with no parent channel still names where its card was posted"
    )


async def test_a_slack_failure_is_logged_and_swallowed() -> None:
    with AioResponsesMock() as mock:
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            "https://slack.com/api/chat.update",
            payload={"ok": False, "error": "message_not_found"},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        with structlog.testing.capture_logs() as logs:
            await edit_posted_card(client, row=_row(), state="received")

    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warnings) == 1, "a failed edit logs exactly once"
    assert warnings[0]["event"] == "posted_card.edit_failed"
    assert warnings[0]["error"] == "message_not_found", "the log names Slack's error code"
    assert warnings[0]["state"] == "received", "the log names the state that failed to render"

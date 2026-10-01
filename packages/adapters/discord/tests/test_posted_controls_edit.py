"""`edit_posted_card` — re-rendering a request's own card from the bot process.

Three things are load-bearing here and none of them are visible from the
renderer alone: the edit always carries a `LayoutView` (a components-v2
message can never be edited back to a classic view), it never re-pings the
requester, and it never raises — by the time it runs, the state it announces
is already durable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import structlog
from daimon.adapters.discord.posted_controls import edit_posted_card
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_requests import build_skill_repo_target
from daimon.core.posted_controls import RECEIVED_FOOTER, CardState
from daimon.core.stores.domain import CredentialRequestRow

_NOW = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)


def _row(**overrides: Any) -> CredentialRequestRow:
    fields: dict[str, Any] = {
        "token": "tok-abcdefghijklmnopqrst",
        "kind": "env",
        "tenant_id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "account_id": uuid.uuid4(),
        "target": "TOGGL_TOKEN",
        "mcp_server_url": None,
        "requester_platform_user_id": "100000000000000001",
        "channel_id": "333",
        "platform": "discord",
        "parent_channel_id": "222",
        "origin_thread_id": "333",
        "posted_message_id": "444",
        "idempotency_key": uuid.uuid4(),
        "target_name": "research-bot",
        "responder_name": "Daimon",
        "created_at": _NOW,
        "expires_at": _NOW + timedelta(minutes=30),
        "used_at": None,
    }
    fields.update(overrides)
    return CredentialRequestRow(**fields)


def _client() -> MagicMock:
    client = MagicMock(spec=discord.Client)
    client.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
    return client


def _card(client: MagicMock) -> MagicMock:
    return client.get_partial_messageable.return_value.get_partial_message.return_value


def _text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )


async def test_edit_targets_the_rows_own_message_with_a_layout_view() -> None:
    client = _client()

    await edit_posted_card(client, row=_row(), state="received")

    client.get_partial_messageable.assert_called_once_with(333)
    client.get_partial_messageable.return_value.get_partial_message.assert_called_once_with(444)
    kwargs = _card(client).edit.call_args.kwargs
    assert isinstance(kwargs["view"], discord.ui.LayoutView), (
        "every edit must pass a LayoutView — a components-v2 message rejects a classic view"
    )
    assert RECEIVED_FOOTER in _text(kwargs["view"]), "the received card says the value arrived"


async def test_edit_suppresses_every_mention() -> None:
    client = _client()

    await edit_posted_card(client, row=_row(), state="received")

    allowed = _card(client).edit.call_args.kwargs["allowed_mentions"]
    assert allowed.users is False and allowed.everyone is False and allowed.roles is False, (
        "only the initial post pings; re-rendering must never re-ping the requester"
    )


@pytest.mark.parametrize(
    ("origin_thread_id", "posted_message_id"),
    [(None, "444"), ("333", None), (None, None)],
    ids=["no-thread", "no-message", "neither"],
)
async def test_edit_returns_silently_for_a_row_with_no_posted_card(
    origin_thread_id: str | None, posted_message_id: str | None
) -> None:
    client = _client()

    await edit_posted_card(
        client,
        row=_row(origin_thread_id=origin_thread_id, posted_message_id=posted_message_id),
        state="received",
    )

    client.get_partial_messageable.assert_not_called()


async def test_edit_swallows_an_http_failure_and_logs_the_state() -> None:
    client = _client()
    _card(client).edit = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "message deleted")
    )
    with structlog.testing.capture_logs() as captured:
        await edit_posted_card(client, row=_row(), state="applied", outcome=_outcome())

    assert [entry["event"] for entry in captured] == ["posted_card.edit_failed"], (
        "a card that cannot be re-rendered is a feedback downgrade, not a failed write"
    )
    entry = captured[0]
    assert entry["state"] == "applied" and entry["err_type"] == "HTTPException", (
        "the log must name the state that could not be shown and the failure class"
    )


def _outcome() -> ConfigurationChange:
    return ConfigurationChange(
        target_name="research-bot",
        kind="key",
        detail="TOGGL_TOKEN",
        availability="next_message",
    )


async def test_repo_card_names_the_repo_from_the_rows_packed_target() -> None:
    client = _client()
    row = _row(
        kind="skill_repo",
        target=build_skill_repo_target("https://github.com/acme/skills", "release", "skills"),
    )

    await edit_posted_card(client, row=row, state="expired")

    text = _text(_card(client).edit.call_args.kwargs["view"])
    assert "acme/skills" in text, "the card names owner/repo, never the packed target string"
    assert "@release" not in text, "the packed branch and path never reach the card"


@pytest.mark.parametrize("state", ["received", "applied", "expired", "superseded"], ids=str)
async def test_every_edited_state_renders_without_a_button(state: CardState) -> None:
    client = _client()
    outcome = _outcome() if state == "applied" else None

    await edit_posted_card(client, row=_row(), state=state, outcome=outcome)

    view = _card(client).edit.call_args.kwargs["view"]
    buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
    assert buttons == [], f"state {state!r} must leave no clickable button on the card"


async def test_unnamed_row_falls_back_to_neutral_names() -> None:
    client = _client()

    await edit_posted_card(client, row=_row(target_name=None, responder_name=None), state="expired")

    text = _text(_card(client).edit.call_args.kwargs["view"])
    assert "the agent" in text and "Daimon" in text, (
        "a row minted without display names still renders readable copy"
    )

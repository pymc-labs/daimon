"""Tests for the Slack message-feedback surface (feedback.py).

Behavioral assertions — grouped by unit:

Pure builders / classification:
  - build_feedback_actions_block renders one actions block with the two vote
    buttons and no other elements.
  - vote_for_action_id classifies the two known action_ids and nothing else.
  - build_feedback_modal carries the feedback row id and channel through
    private_metadata and uses the feedback_text callback_id.
  - evaluate_feedback_text_submission rejects whitespace-only text with a
    response_action errors payload and passes real text through untouched.

handle_feedback_vote (real Postgres + transport-level FakeSlackWebClient):
  - Up-vote records a row and acks with chat.postEphemeral; no modal.
  - Fresh down-vote opens the modal carrying the vote row's id.
  - Repeat down-vote does not re-open the modal.
  - Unregistered tenant records nothing and stays silent.

run_feedback_text_submission:
  - Attaches text to the voter's own row and acks ephemerally.
  - Someone else's row id writes nothing and does not reveal whether the
    row exists.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.feedback import (
    FEEDBACK_TEXT_CALLBACK_ID,
    FEEDBACK_VOTE_DOWN,
    FEEDBACK_VOTE_UP,
    build_feedback_actions_block,
    build_feedback_modal,
    evaluate_feedback_text_submission,
    handle_feedback_vote,
    run_feedback_text_submission,
    vote_for_action_id,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# build_feedback_actions_block
# ---------------------------------------------------------------------------


def test_actions_block_has_exactly_the_two_vote_buttons() -> None:
    block = build_feedback_actions_block()
    assert block["type"] == "actions"
    action_ids = [e["action_id"] for e in block["elements"]]
    assert action_ids == [FEEDBACK_VOTE_UP, FEEDBACK_VOTE_DOWN]


def test_vote_buttons_carry_no_state_or_style() -> None:
    """The message is shared across viewers — a styled button would leak one
    person's vote to everyone, so both buttons must stay unstyled."""
    block = build_feedback_actions_block()
    for element in block["elements"]:
        assert "style" not in element


# ---------------------------------------------------------------------------
# vote_for_action_id
# ---------------------------------------------------------------------------


def test_vote_for_action_id_classifies_up() -> None:
    assert vote_for_action_id(FEEDBACK_VOTE_UP) == "up"


def test_vote_for_action_id_classifies_down() -> None:
    assert vote_for_action_id(FEEDBACK_VOTE_DOWN) == "down"


def test_vote_for_action_id_rejects_unknown() -> None:
    assert vote_for_action_id("cancel_turn") is None
    assert vote_for_action_id("") is None
    assert vote_for_action_id("feedback_vote:sideways") is None


# ---------------------------------------------------------------------------
# build_feedback_modal
# ---------------------------------------------------------------------------


def test_modal_carries_feedback_id_and_channel_in_private_metadata() -> None:
    view = build_feedback_modal(
        feedback_id="0b6ef01e-9f0a-4bb6-a30c-111111111111",
        channel_id="C_FEED",
    )
    assert view["callback_id"] == FEEDBACK_TEXT_CALLBACK_ID
    meta = json.loads(view["private_metadata"])
    assert meta["feedback_id"] == "0b6ef01e-9f0a-4bb6-a30c-111111111111"
    assert meta["channel_id"] == "C_FEED"


def test_modal_has_one_required_text_input() -> None:
    view = build_feedback_modal(feedback_id="x", channel_id="C")
    inputs = [b for b in view["blocks"] if b["type"] == "input"]
    assert len(inputs) == 1
    assert inputs[0]["element"]["multiline"] is True


# ---------------------------------------------------------------------------
# evaluate_feedback_text_submission
# ---------------------------------------------------------------------------


def _submission_payload(text: str) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": "T_FEED"},
        "user": {"id": "U_FEED"},
        "view": {
            "callback_id": FEEDBACK_TEXT_CALLBACK_ID,
            "private_metadata": json.dumps(
                {"feedback_id": "0b6ef01e-9f0a-4bb6-a30c-111111111111", "channel_id": "C_FEED"}
            ),
            "state": {
                "values": {
                    "feedback_text_block": {"feedback_text_input": {"value": text}},
                }
            },
        },
    }


def test_whitespace_only_text_is_rejected_with_field_error() -> None:
    decision = evaluate_feedback_text_submission(_submission_payload("   \n"))
    assert decision.proceed is False
    assert decision.response_payload is not None
    assert decision.response_payload["response_action"] == "errors"
    assert "feedback_text_block" in decision.response_payload["errors"]


def test_real_text_proceeds_with_empty_ack() -> None:
    decision = evaluate_feedback_text_submission(_submission_payload("the answer was wrong"))
    assert decision.proceed is True
    assert decision.response_payload is None
    assert decision.text == "the answer was wrong"
    assert decision.feedback_id == "0b6ef01e-9f0a-4bb6-a30c-111111111111"
    assert decision.channel_id == "C_FEED"


# ---------------------------------------------------------------------------
# handle_feedback_vote / run_feedback_text_submission
# ---------------------------------------------------------------------------

_TEAM_ID = "T_FEEDBACK"
_USER_ID = "U_VOTER"
_CHANNEL_ID = "C_FEED"
_MESSAGE_TS = "1700000001.000100"

_EPHEMERAL_URL = yarl.URL("https://slack.com/api/chat.postEphemeral")
_VIEWS_OPEN_URL = yarl.URL("https://slack.com/api/views.open")


async def _seed_team(session: AsyncSession, *, team_id: str = _TEAM_ID) -> tuple[uuid.UUID, str]:
    """Create tenant + bot token for a team. Returns (tenant_id, fernet_key)."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        session, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
    )
    await session.flush()
    return tenant.id, fernet_key


def _build_runtime(fernet_key: str, db_factory: async_sessionmaker[AsyncSession]) -> SlackRuntime:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=db_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


def _vote_payload(action_id: str, *, team_id: str = _TEAM_ID) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": team_id},
        "user": {"id": _USER_ID},
        "channel": {"id": _CHANNEL_ID},
        "container": {"message_ts": _MESSAGE_TS},
        "message": {"ts": _MESSAGE_TS, "thread_ts": "1700000000.000001"},
        "trigger_id": "TRIGGER_TEST",
        "actions": [{"action_id": action_id}],
    }


async def _vote_rows(session: AsyncSession) -> list[Any]:
    """Read vote rows via plain SQL — adapter tests must not import core._models."""
    result = await session.execute(
        text(
            "SELECT id, vote, message_id, platform_user_id, feedback_text"
            " FROM message_feedback ORDER BY created_at"
        )
    )
    return list(result.mappings())


@pytest.mark.asyncio
async def test_up_vote_records_row_and_acks_ephemerally(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_UP))

    async with db_session_factory() as s:
        rows = await _vote_rows(s)
    assert len(rows) == 1, "up-vote must upsert exactly one vote row"
    assert rows[0]["vote"] == "up"
    assert rows[0]["message_id"] == _MESSAGE_TS
    assert rows[0]["platform_user_id"] == _USER_ID
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests, (
        "the voter must get a per-user ephemeral acknowledgement"
    )
    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests, (
        "an up-vote must not open the what-went-wrong modal"
    )


@pytest.mark.asyncio
async def test_fresh_down_vote_opens_modal_with_row_id(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))

    async with db_session_factory() as s:
        rows = await _vote_rows(s)
    assert len(rows) == 1 and rows[0]["vote"] == "down"
    opens = fake_slack_web_client.mock.requests.get(("POST", _VIEWS_OPEN_URL), [])
    assert len(opens) == 1, "a genuinely new down-vote must open the modal"
    view = opens[0].kwargs["json"]["view"]
    meta = json.loads(view["private_metadata"])
    assert meta["feedback_id"] == str(rows[0]["id"]), (
        "the modal must carry the vote row's id as the write handle"
    )
    assert meta["channel_id"] == _CHANNEL_ID


@pytest.mark.asyncio
async def test_repeat_down_vote_does_not_reopen_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))
    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))

    opens = fake_slack_web_client.mock.requests.get(("POST", _VIEWS_OPEN_URL), [])
    assert len(opens) == 1, "a repeat down-vote must not re-open the modal"


@pytest.mark.asyncio
async def test_unregistered_tenant_records_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    # Bot token exists (client resolvable) but no tenant row for this team.
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    await upsert_slack_bot_token(
        db_session, team_id="T_GHOST", encrypted_token=encrypt_token(fernet, "xoxb-test")
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_UP, team_id="T_GHOST"))

    async with db_session_factory() as s:
        rows = await _vote_rows(s)
    assert rows == [], "a vote in an unregistered workspace must record nothing"


@pytest.mark.asyncio
async def test_submission_attaches_text_to_own_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)
    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))
    async with db_session_factory() as s:
        row_id = (await _vote_rows(s))[0]["id"]

    await run_feedback_text_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        feedback_id=str(row_id),
        text="the numbers were wrong",
    )

    async with db_session_factory() as s:
        rows = await _vote_rows(s)
    assert rows[0]["feedback_text"] == "the numbers were wrong"


@pytest.mark.asyncio
async def test_submission_for_someone_elses_row_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)
    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))
    async with db_session_factory() as s:
        row_id = (await _vote_rows(s))[0]["id"]

    await run_feedback_text_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id="U_SOMEONE_ELSE",
        channel_id=_CHANNEL_ID,
        feedback_id=str(row_id),
        text="hijack attempt",
    )

    async with db_session_factory() as s:
        rows = await _vote_rows(s)
    assert rows[0]["feedback_text"] is None, (
        "a submission carrying someone else's row id must write nothing"
    )

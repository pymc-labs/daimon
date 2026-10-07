"""Tests for the Slack message-feedback surface (feedback.py).

Pure builders / classification:
  - the actions block carries exactly the two unstyled vote buttons;
  - vote_for_action_id classifies the two known action_ids and nothing else;
  - the "What went wrong?" form carries the answer's place (never a row id or
    identity), offers the reason vocabulary, and both inputs are optional;
  - evaluate_feedback_text_submission needs a reason or text, drops unknown
    reason codes, and refuses an external Slack Connect submitter.

handle_feedback_vote (real Postgres + transport-level FakeSlackWebClient):
  - 👍 records and acks ephemerally in the thread; no form;
  - every 👎 opens the form BEFORE any database work, repeats included;
  - a repeat 👎 leaves one row (concurrent clicks: test_feedback_races.py);
  - a form Slack refused to open falls back to an ephemeral with a button;
  - a voter the tenant would not let start a turn there records nothing and
    sees why (the form is replaced);
  - an unregistered workspace records nothing;
  - thread replies and DM answers both record against the answer's ts, and a
    top-level answer's ephemeral is not aimed at a thread;
  - a views.open timeout still records the vote and offers the button.

run_feedback_text_submission:
  - records the down-vote and attaches text and reasons on the submitter's row;
  - reason-only forms store NULL text; a later form replaces both fields;
  - someone else's answer only ever reaches the submitter's own row;
  - a refused submitter records nothing;
  - a form opened before this change (row id only) writes nothing and says to
    start again, since its place can't be authorized.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import pytest
import yarl
from aioresponses import aioresponses as AioResponsesMock
from cryptography.fernet import Fernet
from daimon.adapters.slack import feedback as feedback_module
from daimon.adapters.slack.click_replies import open_modal
from daimon.adapters.slack.feedback import (
    FEEDBACK_DETAILS_ACTION_ID,
    FEEDBACK_TEXT_CALLBACK_ID,
    FEEDBACK_VOTE_DOWN,
    FEEDBACK_VOTE_UP,
    build_feedback_actions_block,
    build_feedback_modal,
    evaluate_feedback_text_submission,
    handle_feedback_details_click,
    handle_feedback_vote,
    run_feedback_text_submission,
    vote_for_action_id,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.message_feedback import FEEDBACK_REASONS
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import build_slack_runtime

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


def _modal() -> dict[str, Any]:
    return build_feedback_modal(channel_id="C_FEED", message_ts="1.2", thread_ts="1.0")


def test_modal_carries_the_answer_place_and_nothing_else() -> None:
    view = _modal()
    assert view["callback_id"] == FEEDBACK_TEXT_CALLBACK_ID
    assert json.loads(view["private_metadata"]) == {
        "channel_id": "C_FEED",
        "message_ts": "1.2",
        "thread_ts": "1.0",
    }


def test_modal_offers_the_reason_vocabulary_and_both_inputs_are_optional() -> None:
    view = _modal()
    inputs = [b for b in view["blocks"] if b["type"] == "input"]
    assert [b["element"]["type"] for b in inputs] == ["checkboxes", "plain_text_input"]
    assert all(b["optional"] is True for b in inputs)
    offered = [o["value"] for o in inputs[0]["element"]["options"]]
    assert offered == list(FEEDBACK_REASONS)


# ---------------------------------------------------------------------------
# evaluate_feedback_text_submission
# ---------------------------------------------------------------------------


def _submission_payload(
    text: str,
    *,
    reasons: tuple[str, ...] = (),
    message_ts: str = "1700000001.000100",
    user_team: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user: dict[str, Any] = {"id": "U_FEED"}
    if user_team is not None:
        user["team_id"] = user_team
    return {
        "type": "view_submission",
        "team": {"id": "T_FEED"},
        "user": user,
        "view": {
            "callback_id": FEEDBACK_TEXT_CALLBACK_ID,
            "private_metadata": json.dumps(
                meta
                if meta is not None
                else {"channel_id": "C_FEED", "message_ts": message_ts, "thread_ts": "1.0"}
            ),
            "state": {
                "values": {
                    "feedback_text_block": {"feedback_text_input": {"value": text}},
                    "feedback_reasons_block": {
                        "feedback_reasons_input": {
                            "selected_options": [{"value": r} for r in reasons]
                        }
                    },
                }
            },
        },
    }


def test_a_form_with_neither_reason_nor_text_is_refused_with_a_field_error() -> None:
    decision = evaluate_feedback_text_submission(_submission_payload("   \n"))
    assert decision.proceed is False
    assert decision.response_payload is not None
    assert decision.response_payload["response_action"] == "errors"
    assert "feedback_text_block" in decision.response_payload["errors"]


def test_text_alone_proceeds_with_empty_ack() -> None:
    decision = evaluate_feedback_text_submission(_submission_payload("the answer was wrong"))
    assert decision.proceed is True
    assert decision.response_payload is None
    assert decision.text == "the answer was wrong"
    assert decision.channel_id == "C_FEED"
    assert decision.message_ts == "1700000001.000100"
    assert decision.thread_ts == "1.0"


def test_a_reason_alone_proceeds() -> None:
    decision = evaluate_feedback_text_submission(_submission_payload("", reasons=("too_slow",)))
    assert decision.proceed is True
    assert decision.reasons == ("too_slow",)


def test_unknown_reason_codes_are_dropped_and_known_ones_kept_in_order() -> None:
    decision = evaluate_feedback_text_submission(
        _submission_payload("", reasons=("other", "<script>", "inaccurate", "other"))
    )
    assert decision.reasons == ("inaccurate", "other")


def test_only_unknown_reason_codes_count_as_no_reason() -> None:
    decision = evaluate_feedback_text_submission(_submission_payload("", reasons=("bogus",)))
    assert decision.proceed is False


def test_an_external_slack_connect_submission_closes_and_goes_no_further() -> None:
    decision = evaluate_feedback_text_submission(
        _submission_payload("wrong", user_team="T_ELSEWHERE")
    )
    assert decision.proceed is False
    assert decision.response_payload is None, "the form closes"


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

_TEAM_ID = "T_FEEDBACK"
_USER_ID = "U_VOTER"
_CHANNEL_ID = "C_FEED"
_THREAD_TS = "1700000000.000001"
_MESSAGE_TS = "1700000001.000100"

_EPHEMERAL_URL = yarl.URL("https://slack.com/api/chat.postEphemeral")
_VIEWS_OPEN_URL = yarl.URL("https://slack.com/api/views.open")
_VIEWS_UPDATE_URL = yarl.URL("https://slack.com/api/views.update")


async def _seed_team(
    session: AsyncSession, *, team_id: str = _TEAM_ID, policy: TenantAccessPolicy | None = None
) -> tuple[uuid.UUID, str]:
    """Create tenant + bot token for a team. Returns (tenant_id, fernet_key)."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        session, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
    )
    if policy is not None:
        await set_access_policy(session, tenant_id=tenant.id, policy=policy)
    await session.flush()
    return tenant.id, fernet_key


def _vote_payload(
    action_id: str,
    *,
    team_id: str = _TEAM_ID,
    user_id: str = _USER_ID,
    channel_id: str = _CHANNEL_ID,
    message_ts: str = _MESSAGE_TS,
    thread_ts: str | None = _THREAD_TS,
) -> dict[str, Any]:
    message: dict[str, Any] = {"ts": message_ts}
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    return {
        "type": "block_actions",
        "team": {"id": team_id},
        "user": {"id": user_id, "team_id": team_id},
        "channel": {"id": channel_id},
        "container": {"message_ts": message_ts, "channel_id": channel_id},
        "message": message,
        "trigger_id": "TRIGGER_TEST",
        "actions": [{"action_id": action_id}],
    }


async def _vote_rows(session: AsyncSession) -> list[Any]:
    """Read vote rows via plain SQL — adapter tests must not import core._models."""
    result = await session.execute(
        text(
            "SELECT id, vote, message_id, channel_id, platform_user_id, feedback_text,"
            " feedback_reasons FROM message_feedback ORDER BY created_at"
        )
    )
    return list(result.mappings())


async def _rows(factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with factory() as s:
        return await _vote_rows(s)


def _calls(fake: Any, url: yarl.URL) -> list[dict[str, Any]]:
    return [c.kwargs["json"] for c in fake.mock.requests.get(("POST", url), [])]


async def _runtime(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    *,
    policy: TenantAccessPolicy | None = None,
) -> Any:
    _tenant_id, fernet_key = await _seed_team(db_session, policy=policy)
    await db_session.commit()
    return build_slack_runtime(fernet_key, factory)


async def test_up_vote_records_row_and_acks_ephemerally_in_the_thread(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)

    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_UP))

    rows = await _rows(db_session_factory)
    assert len(rows) == 1, "up-vote must upsert exactly one vote row"
    assert rows[0]["vote"] == "up"
    assert rows[0]["message_id"] == _MESSAGE_TS
    assert rows[0]["platform_user_id"] == _USER_ID
    (ephemeral,) = _calls(fake_slack_web_client, _EPHEMERAL_URL)
    assert ephemeral["user"] == _USER_ID
    assert ephemeral["thread_ts"] == _THREAD_TS, "the acknowledgement lands in the thread"
    assert _calls(fake_slack_web_client, _VIEWS_OPEN_URL) == [], "an up-vote opens no form"


async def test_down_vote_opens_the_form_before_any_database_work(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)
    rows_when_opened: list[int] = []
    real_open = feedback_module.open_modal

    async def spying_open(client: Any, *, trigger_id: str, view: dict[str, Any]) -> str | None:
        rows_when_opened.append(len(await _rows(db_session_factory)))
        return await real_open(client, trigger_id=trigger_id, view=view)

    with patch.object(feedback_module, "open_modal", new=spying_open):
        await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))

    assert rows_when_opened == [0], "the 3-second trigger is spent on views.open first"
    rows = await _rows(db_session_factory)
    assert len(rows) == 1 and rows[0]["vote"] == "down"
    (opened,) = _calls(fake_slack_web_client, _VIEWS_OPEN_URL)
    meta = json.loads(opened["view"]["private_metadata"])
    assert meta == {"channel_id": _CHANNEL_ID, "message_ts": _MESSAGE_TS, "thread_ts": _THREAD_TS}
    assert _calls(fake_slack_web_client, _EPHEMERAL_URL) == [], "the form is the acknowledgement"


async def test_a_repeat_down_vote_opens_the_form_again_on_the_same_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)

    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))
    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))

    assert len(_calls(fake_slack_web_client, _VIEWS_OPEN_URL)) == 2, (
        "a second thumbs-down still lets the person say what went wrong"
    )
    assert len(await _rows(db_session_factory)) == 1


async def test_a_form_slack_would_not_open_falls_back_to_an_ephemeral_with_a_button(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)

    async def refused(client: Any, *, trigger_id: str, view: dict[str, Any]) -> str | None:
        return None

    with patch.object(feedback_module, "open_modal", new=refused):
        await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))

    assert len(await _rows(db_session_factory)) == 1, "the vote still counts"
    (ephemeral,) = _calls(fake_slack_web_client, _EPHEMERAL_URL)
    assert ephemeral["thread_ts"] == _THREAD_TS
    buttons = [e for b in ephemeral["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert [b["action_id"] for b in buttons] == [FEEDBACK_DETAILS_ACTION_ID]
    assert json.loads(buttons[0]["value"])["message_ts"] == _MESSAGE_TS


async def test_the_details_button_opens_the_form_for_that_answer(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)
    place = {"channel_id": _CHANNEL_ID, "message_ts": _MESSAGE_TS, "thread_ts": _THREAD_TS}
    payload = _vote_payload(FEEDBACK_DETAILS_ACTION_ID)
    payload["actions"] = [{"action_id": FEEDBACK_DETAILS_ACTION_ID, "value": json.dumps(place)}]

    await handle_feedback_details_click(runtime, payload)

    (opened,) = _calls(fake_slack_web_client, _VIEWS_OPEN_URL)
    assert json.loads(opened["view"]["private_metadata"]) == place
    assert await _rows(db_session_factory) == [], "opening the form records nothing"


@pytest.mark.parametrize(
    "policy",
    [
        TenantAccessPolicy(invoker_user_ids=("U_SOMEONE_ELSE",)),
        TenantAccessPolicy(protected_channel_ids=(_CHANNEL_ID,)),
    ],
    ids=["outside-invoker-allowlist", "protected-channel"],
)
async def test_a_voter_who_could_not_ask_here_records_nothing_and_is_told(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    policy: TenantAccessPolicy,
) -> None:
    runtime = await _runtime(db_session, db_session_factory, policy=policy)

    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))
    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_UP))

    assert await _rows(db_session_factory) == []
    (update,) = _calls(fake_slack_web_client, _VIEWS_UPDATE_URL)
    assert "callback_id" not in update["view"], "the open form is replaced, not left to submit"
    assert update["view"]["blocks"][0]["text"]["text"] == "You can't leave feedback on this answer."
    (ephemeral,) = _calls(fake_slack_web_client, _EPHEMERAL_URL)
    assert ephemeral["text"] == "You can't leave feedback on this answer.", (
        "the up-vote is told too"
    )


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
    runtime = build_slack_runtime(fernet_key, db_session_factory)

    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_UP, team_id="T_GHOST"))

    assert await _rows(db_session_factory) == [], "an unregistered workspace records nothing"
    assert _calls(fake_slack_web_client, _EPHEMERAL_URL) == []


async def test_a_top_level_answer_in_a_dm_records_against_its_own_ts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)

    await handle_feedback_vote(
        runtime,
        _vote_payload(FEEDBACK_VOTE_DOWN, channel_id="D_DM", message_ts="1.5", thread_ts=None),
    )

    (row,) = await _rows(db_session_factory)
    assert (row["channel_id"], row["message_id"]) == ("D_DM", "1.5")
    (opened,) = _calls(fake_slack_web_client, _VIEWS_OPEN_URL)
    assert json.loads(opened["view"]["private_metadata"]) == {
        "channel_id": "D_DM",
        "message_ts": "1.5",
        "thread_ts": "1.5",
    }


def _decision(**kw: Any) -> Any:
    decision = evaluate_feedback_text_submission(_submission_payload(**kw))
    assert decision.proceed
    return decision


async def _submit(runtime: Any, *, user_id: str = _USER_ID, **kw: Any) -> None:
    kw.setdefault("text", "")
    await run_feedback_text_submission(
        runtime, team_id=_TEAM_ID, user_id=user_id, decision=_decision(**kw)
    )


async def test_submission_records_the_down_vote_with_text_and_reasons(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)

    await _submit(runtime, text="the numbers were wrong", reasons=("inaccurate", "incomplete"))

    (row,) = await _rows(db_session_factory)
    assert row["vote"] == "down", "submitting the form is a down-vote even with no click write"
    assert row["feedback_text"] == "the numbers were wrong"
    assert row["feedback_reasons"] == ["inaccurate", "incomplete"]
    (ephemeral,) = _calls(fake_slack_web_client, _EPHEMERAL_URL)
    assert ephemeral["text"] == "Thanks — your feedback has been recorded."
    assert ephemeral["thread_ts"] == "1.0"


async def test_a_later_form_replaces_the_earlier_one_on_the_same_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)

    await _submit(runtime, text="wrong", reasons=("inaccurate",))
    await _submit(runtime, reasons=("too_slow",))

    (row,) = await _rows(db_session_factory)
    assert row["feedback_text"] is None
    assert row["feedback_reasons"] == ["too_slow"]


async def test_two_submissions_for_one_answer_leave_one_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)
    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))

    await _submit(runtime, text="one")
    await _submit(runtime, text="two")

    rows = await _rows(db_session_factory)
    assert len(rows) == 1 and rows[0]["feedback_text"] == "two"


async def test_a_submission_only_ever_reaches_the_submitters_own_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)
    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))

    await _submit(runtime, user_id="U_SOMEONE_ELSE", text="hijack attempt")

    rows = {r["platform_user_id"]: r for r in await _rows(db_session_factory)}
    assert rows[_USER_ID]["feedback_text"] is None, "the voter's row is untouched"
    assert rows["U_SOMEONE_ELSE"]["feedback_text"] == "hijack attempt"


async def test_a_refused_submitter_records_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(
        db_session, db_session_factory, policy=TenantAccessPolicy(invoker_user_ids=("U_X",))
    )

    await _submit(runtime, text="wrong")

    assert await _rows(db_session_factory) == []
    (ephemeral,) = _calls(fake_slack_web_client, _EPHEMERAL_URL)
    assert ephemeral["text"] == "You can't leave feedback on this answer."


async def test_a_form_opened_before_this_change_writes_nothing_and_says_start_again(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Its metadata holds only a row id, so access can't be decided again."""
    runtime = await _runtime(db_session, db_session_factory)
    await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))
    row_id = str((await _rows(db_session_factory))[0]["id"])

    await _submit(
        runtime,
        text="the numbers were wrong",
        meta={"feedback_id": row_id, "channel_id": _CHANNEL_ID},
    )

    (row,) = await _rows(db_session_factory)
    assert row["vote"] == "down", "the click-time vote stands"
    assert row["feedback_text"] is None
    (ephemeral,) = _calls(fake_slack_web_client, _EPHEMERAL_URL)
    assert "expired" in ephemeral["text"]


async def test_a_views_open_timeout_still_records_the_vote_and_offers_the_button(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    runtime = await _runtime(db_session, db_session_factory)

    async def timing_out(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError

    with patch.object(AsyncWebClient, "views_open", new=timing_out):
        await handle_feedback_vote(runtime, _vote_payload(FEEDBACK_VOTE_DOWN))

    (row,) = await _rows(db_session_factory)
    assert row["vote"] == "down"
    (ephemeral,) = _calls(fake_slack_web_client, _EPHEMERAL_URL)
    assert ephemeral["blocks"][-1]["elements"][0]["action_id"] == FEEDBACK_DETAILS_ACTION_ID


async def test_a_top_level_answer_gets_its_ephemeral_outside_any_thread(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Slack drops a threaded ephemeral aimed at a message with no thread."""
    runtime = await _runtime(db_session, db_session_factory)

    await handle_feedback_vote(
        runtime,
        _vote_payload(FEEDBACK_VOTE_UP, channel_id="D_DM", message_ts="1.5", thread_ts=None),
    )

    (ephemeral,) = _calls(fake_slack_web_client, _EPHEMERAL_URL)
    assert ephemeral["channel"] == "D_DM"
    assert "thread_ts" not in ephemeral


async def test_open_modal_returns_none_when_slack_refuses() -> None:
    with AioResponsesMock() as mock:
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            str(_VIEWS_OPEN_URL), payload={"ok": False, "error": "expired_trigger_id"}
        )
        client = AsyncWebClient(token="xoxb-test")
        assert await open_modal(client, trigger_id="T", view=_modal()) is None

"""Support plans on Slack: the menu, option forms, admin exemption and settling a post.

Real Postgres plus the transport-level fake Slack, as `test_support_escalation.py`.
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack import support_escalation as slack_support
from daimon.adapters.slack.support_escalation import (
    ASK_HUMAN_ACTION_ID,
    SUPPORT_CALLBACK_ID,
    evaluate_plan_submission,
    evaluate_support_submission,
    handle_ask_human_click,
    handle_option_pick,
    run_support_submission,
)
from daimon.adapters.slack.support_plan_form import (
    OPTION_ACTION_ID,
    OPTION_BLOCK_ID,
    PLAN_CALLBACK_ID,
)
from daimon.adapters.slack.support_post import (
    NOT_NOTIFIED_LINE,
    SETTLE_DECLINE_ACTION_ID,
    SETTLE_SET_ACTION_ID,
)
from daimon.adapters.slack.support_settle import (
    SETTLE_CALLBACK_ID,
    evaluate_settle_submission,
    handle_settle_click,
    run_settle_submission,
)
from daimon.core.config import SupportSettings
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores import support_plans as plans
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.support_plans import (
    CLIENT_NOT_NOTIFIED,
    EXEMPT_OFFER,
    NOT_ENOUGH_CREDITS,
    OPTIONS_CHANGED,
    SETTLE_ELSEWHERE,
    parse_catalog,
)
from daimon.core.windows import WindowBounds
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import CHAT_OK_PAYLOAD, shown_text
from .harness import build_slack_runtime

pytestmark = pytest.mark.asyncio

_TEAM = "T_SUP"
_USER = "U_ASKER"
_STAFF = "U0STAFF"
_CHANNEL = "C_ANSWERS"
_THREAD = "1700000000.000001"
_ANSWER_TS = "1700000001.000100"
_ESC_CHANNEL = "C_ESCALATE"
_POST_TS = str(CHAT_OK_PAYLOAD["ts"])
_PERMALINK = "https://sup.slack.com/archives/C_ANSWERS/p1700000001000100"
D = Decimal
_ALLOWANCE = D(2)

_EPHEMERAL = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
_POST = ("POST", yarl.URL("https://slack.com/api/chat.postMessage"))
_UPDATE = ("POST", yarl.URL("https://slack.com/api/chat.update"))
_VIEWS_OPEN = ("POST", yarl.URL("https://slack.com/api/views.open"))
_VIEWS_UPDATE = ("POST", yarl.URL("https://slack.com/api/views.update"))
_PERMALINK_PATTERN = re.compile(r"https://slack\.com/api/chat\.getPermalink.*")
_HISTORY_PATTERN = re.compile(r"https://slack\.com/api/conversations\.history.*")

MENU = parse_catalog(
    {
        "name": "Support",
        "options": [
            {"key": "quick", "label": "Quick", "price": {"kind": "fixed", "amount": "0.5"}},
            {
                "key": "review",
                "label": "Review",
                "price": {"kind": "range", "min": "1", "max": "3"},
            },
            {
                "key": "call",
                "label": "Call",
                "price": {
                    "kind": "steps",
                    "label": "Length",
                    "steps": [
                        {"label": "30 min", "amount": "0.5"},
                        {"label": "1 hour", "amount": "1"},
                    ],
                },
                "fields": [
                    {"key": "topic", "kind": "text", "label": "Topic"},
                    {
                        "key": "when",
                        "kind": "choice",
                        "label": "When",
                        "choices": ["Today", "This week"],
                        "required": False,
                    },
                ],
            },
        ],
    }
)


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    menu: bool = True,
    per_person: Decimal = _ALLOWANCE,
) -> tuple[str, int]:
    """A Slack tenant with a workspace plan; its fernet key and the menu's revision."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    async with factory() as session, session.begin():
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM)
        await upsert_slack_bot_token(
            session, team_id=_TEAM, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        catalog = None
        if menu:
            catalog = await plans.set_catalog(
                session, tenant_id=tenant.id, key="support", spec=MENU, set_by_account_id=None
            )
        await plans.set_plan(
            session,
            tenant_id=tenant.id,
            platform="slack",
            scope="workspace",
            scope_id="",
            catalog_id=None if catalog is None else catalog.id,
            pool=None,
            per_person=per_person,
            window=WindowBounds("total", None, None),
            set_by_account_id=None,
        )
    return fernet_key, 0 if catalog is None else catalog.revision


def _runtime(fernet_key: str, factory: async_sessionmaker[AsyncSession], **support: Any) -> Any:
    settings = MagicMock()
    support = {"slack_escalation_team_id": _TEAM, **support}
    settings.support = SupportSettings(slack_escalation_channel_id=_ESC_CHANNEL, **support)
    return build_slack_runtime(fernet_key, factory, settings=settings)


@pytest.fixture
def fake(fake_slack_web_client: Any) -> Any:
    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        _PERMALINK_PATTERN, payload={"ok": True, "permalink": _PERMALINK}, repeat=True
    )
    return fake_slack_web_client


def _sent(fake: Any, key: tuple[str, yarl.URL]) -> list[dict[str, Any]]:
    return [c.kwargs["json"] for c in fake.mock.requests.get(key, [])]


def _shown(fake: Any) -> list[dict[str, Any]]:
    """Every view put in front of the person: the opens, then each update."""
    return [b["view"] for b in _sent(fake, _VIEWS_OPEN) + _sent(fake, _VIEWS_UPDATE)]


def _opened(fake: Any) -> dict[str, Any]:
    """The last view opened: the Set charge form."""
    return _sent(fake, _VIEWS_OPEN)[-1]["view"]


def _said(fake: Any) -> str:
    """The last ephemeral's text."""
    return shown_text(_sent(fake, _EPHEMERAL)[-1])


async def _rows(factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT id, status, hold, charge, support_post_id, answers, settled_by"
                " FROM support_escalations ORDER BY created_at"
            )
        )
        return list(result.all())


def _click() -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM},
        "user": {"id": _USER, "team_id": _TEAM},
        "channel": {"id": _CHANNEL},
        "container": {"message_ts": _ANSWER_TS, "channel_id": _CHANNEL},
        "message": {"ts": _ANSWER_TS, "thread_ts": _THREAD},
        "trigger_id": "TRIGGER",
        "actions": [{"action_id": ASK_HUMAN_ACTION_ID}],
    }


async def _menu(runtime: Any, fake: Any) -> dict[str, Any]:
    await handle_ask_human_click(runtime, _click())
    return _shown(fake)[-1]


async def _pick(runtime: Any, fake: Any, view: dict[str, Any], key: str) -> dict[str, Any]:
    await handle_option_pick(
        runtime,
        {
            "type": "block_actions",
            "team": {"id": _TEAM},
            "user": {"id": _USER, "team_id": _TEAM},
            "view": {"id": "V_TEST", "private_metadata": view["private_metadata"]},
            "actions": [{"action_id": OPTION_ACTION_ID, "selected_option": {"value": key}}],
        },
    )
    return _shown(fake)[-1]


def _picked(key: str) -> dict[str, Any]:
    return {OPTION_ACTION_ID: {"selected_option": {"value": key}}}


def _send(view: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": _TEAM},
        "user": {"id": _USER, "team_id": _TEAM},
        "view": {
            "callback_id": view["callback_id"],
            "private_metadata": view["private_metadata"],
            "state": {"values": values},
        },
    }


async def _pending(runtime: Any, fake: Any) -> tuple[str, dict[str, Any]]:
    """A pending Review request: its id and the support post as Slack holds it."""
    picked = await _pick(runtime, fake, await _menu(runtime, fake), "review")
    sent = evaluate_plan_submission(_send(picked, {OPTION_BLOCK_ID: _picked("review")}))
    await run_support_submission(runtime, sent)
    post = _sent(fake, _POST)[-1]
    return str(post["blocks"][-1]["elements"][0]["value"]), {
        "ts": _POST_TS,
        "text": post["text"],
        "blocks": post["blocks"],
    }


def _settle_click(
    action_id: str, value: str, *, message: dict[str, Any], channel: str = _ESC_CHANNEL
) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM},
        "user": {"id": _STAFF, "team_id": _TEAM},
        "channel": {"id": channel},
        "container": {"message_ts": _POST_TS, "channel_id": channel},
        "message": message,
        "trigger_id": "TRIGGER",
        "actions": [{"action_id": action_id, "value": value}],
    }


def _charge(view: dict[str, Any], amount: str) -> dict[str, Any]:
    values = {"support_charge_block": {"support_charge_input": {"value": amount}}}
    payload = _send(view, values)
    payload["user"] = {"id": _STAFF, "team_id": _TEAM}
    return payload


async def test_click_when_plan_has_a_menu_then_shows_options_and_balance(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, revision = await _seed(db_session_factory)
    view = await _menu(_runtime(key, db_session_factory), fake)
    assert view["callback_id"] == PLAN_CALLBACK_ID
    radio = next(b for b in view["blocks"] if b.get("block_id") == OPTION_BLOCK_ID)["element"]
    assert [o["value"] for o in radio["options"]] == ["quick", "review", "call"]
    assert radio["options"][1]["description"]["text"] == "1-3 credits", "each option's price"
    assert "2 of 2 credits left" in json.dumps(view), "the balance sits under the menu"
    assert json.loads(view["private_metadata"])["revision"] == revision
    assert await _rows(db_session_factory) == [], "opening the menu spends nothing"


async def test_pick_when_option_has_steps_and_fields_then_its_form_shows_them(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    view = await _pick(runtime, fake, await _menu(runtime, fake), "call")
    ids = [b.get("block_id") for b in view["blocks"] if b["type"] == "input"]
    assert ids == [OPTION_BLOCK_ID, "support_step:call", "support_field:call:topic"] + [
        "support_field:call:when"
    ], "the pick, its step and its two fields"
    assert json.loads(view["private_metadata"])["option"] == "call"


async def test_pick_when_the_menu_changed_then_options_changed(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    view = await _menu(runtime, fake)
    stale = {**json.loads(view["private_metadata"]), "revision": revision - 1}
    shown = await _pick(runtime, fake, {"private_metadata": json.dumps(stale)}, "call")
    assert "callback_id" not in shown and OPTIONS_CHANGED in json.dumps(shown)


async def test_pick_when_balance_cannot_cover_it_then_send_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory, per_person=D("0.5"))
    runtime = _runtime(key, db_session_factory)
    view = await _pick(runtime, fake, await _menu(runtime, fake), "review")
    assert NOT_ENOUGH_CREDITS in json.dumps(view), "the menu says why"
    sent = evaluate_plan_submission(_send(view, {OPTION_BLOCK_ID: _picked("review")}))
    assert not sent.proceed
    assert sent.response_payload == {
        "response_action": "errors",
        "errors": {OPTION_BLOCK_ID: NOT_ENOUGH_CREDITS},
    }


async def test_send_when_option_has_no_fields_then_posts_pending_with_settle_buttons(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    escalation_id, post = await _pending(_runtime(key, db_session_factory), fake)
    assert "Review: 1-3 credits\nPending: 1-3 credits\n1 of 2 credits left" in post["text"], (
        "a plan request's post names its terms, status and balance"
    )
    assert NOT_NOTIFIED_LINE in post["text"], "decision 8 beside the settle buttons"
    buttons = post["blocks"][-1]["elements"]
    assert [b["action_id"] for b in buttons] == [SETTLE_SET_ACTION_ID, SETTLE_DECLINE_ACTION_ID]
    assert (
        _said(fake)
        == "Request sent. Someone will reply.\nHolding 1 credit. We will confirm the charge."
    )
    ((row_id, status, hold, _charge, post_id, _answers, _by),) = await _rows(db_session_factory)
    assert row_id.hex == escalation_id
    assert (status, hold, post_id) == ("pending", D(1), _POST_TS), "holds its minimum; post id"


async def test_decline_when_no_escalation_workspace_is_named_then_the_requests_own_workspace_settles(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory, slack_escalation_team_id=None)
    escalation_id, post = await _pending(runtime, fake)
    assert [b["action_id"] for b in post["blocks"][-1]["elements"]] == [
        SETTLE_SET_ACTION_ID,
        SETTLE_DECLINE_ACTION_ID,
    ], "the post carries the settle buttons"
    fake.mock.get(_HISTORY_PATTERN, payload={"ok": True, "messages": [post]}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
    await handle_settle_click(
        runtime, _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=post)
    )
    assert _said(fake) == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}"
    ((_id, status, *_rest),) = await _rows(db_session_factory)
    assert status == "declined"


async def test_settle_when_no_escalation_workspace_is_named_then_another_workspace_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory, slack_escalation_team_id=None)
    escalation_id, post = await _pending(runtime, fake)
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform="slack", workspace_id="T_OTHER")
        await upsert_slack_bot_token(
            session,
            team_id="T_OTHER",
            encrypted_token=encrypt_token(build_multifernet((key,)), "xoxb-other"),
        )
    click = _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=post)
    click["team"] = {"id": "T_OTHER"}
    click["user"] = {"id": _STAFF, "team_id": "T_OTHER"}
    await handle_settle_click(runtime, click)
    assert _said(fake) == SETTLE_ELSEWHERE
    ((_id, status, *_rest),) = await _rows(db_session_factory)
    assert status == "pending", "nothing settled"


async def test_click_when_another_option_was_asked_on_the_answer_then_the_menu_shows_again(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    await _pending(runtime, fake)
    again = await _menu(runtime, fake)
    blocks = [b.get("block_id") for b in again.get("blocks", [])]
    assert OPTION_BLOCK_ID in blocks, "a menu asks once per option, not once per answer"


async def test_send_when_option_has_steps_and_fields_then_charged_at_submit(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    view = await _pick(runtime, fake, await _menu(runtime, fake), "call")
    values = {
        OPTION_BLOCK_ID: _picked("call"),
        "support_step:call": {"support_step_input": {"selected_option": {"value": "1"}}},
        "support_field:call:topic": {"support_field_input": {"value": "Billing export"}},
    }
    await run_support_submission(runtime, evaluate_plan_submission(_send(view, values)))
    post = _sent(fake, _POST)[-1]
    assert "Call, 1 hour: 1 credit\nCharged 1 credit" in post["text"], "a step is charged now"
    assert "Topic: Billing export" in post["text"], "the answers are the note"
    assert "blocks" not in post, "nothing left to settle"
    assert _said(fake) == (
        "Request sent. Someone will reply.\nUsed 1 credit. 1 of 2 credits left."
    ), "the confirmation says what it used and what is left"


async def test_default_note_when_requester_is_admin_then_uses_no_credits(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, _revision = await _seed(db_session_factory, menu=False)
    runtime = _runtime(key, db_session_factory)
    monkeypatch.setattr(slack_support, "resolve_is_platform_admin", AsyncMock(return_value=True))
    view = await _menu(runtime, fake)
    assert view["callback_id"] == SUPPORT_CALLBACK_ID and EXEMPT_OFFER in json.dumps(view)
    values = {"support_note_block": {"support_note_input": {"value": "help please"}}}
    await run_support_submission(runtime, evaluate_support_submission(_send(view, values)))
    assert "No charge: an admin asked" in _sent(fake, _POST)[-1]["text"]
    assert _said(fake) == "Request sent. Someone will reply.\nThis used no credits."
    ((_id, status, hold, *_rest),) = await _rows(db_session_factory)
    assert (status, hold) == ("settled", D(0)), "an admin's request holds nothing"


async def test_set_charge_when_in_range_then_charges_and_closes_the_post(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    fake.mock.get(_HISTORY_PATTERN, payload={"ok": True, "messages": [post]}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
    await handle_settle_click(
        runtime, _settle_click(SETTLE_SET_ACTION_ID, escalation_id, message=post)
    )
    form = _opened(fake)
    assert form["callback_id"] == SETTLE_CALLBACK_ID
    assert form["blocks"][-1]["element"]["initial_value"] == "1", "prefilled with the hold"
    assert CLIENT_NOT_NOTIFIED in json.dumps(form)
    await run_settle_submission(runtime, evaluate_settle_submission(_charge(form, "2")))
    edited = _sent(fake, _UPDATE)[-1]
    assert (edited["channel"], edited["ts"]) == (_ESC_CHANNEL, _POST_TS)
    assert "Charged 2 credits by <@U0STAFF>" in edited["text"]
    assert NOT_NOTIFIED_LINE not in edited["text"], "the pending warning goes with the buttons"
    assert all(b["type"] != "actions" for b in edited["blocks"]), "the buttons go"
    assert _said(fake) == f"Charged 2 credits. {CLIENT_NOT_NOTIFIED}"
    closed = {"ts": _POST_TS, "text": edited["text"], "blocks": edited["blocks"]}
    await handle_settle_click(
        runtime, _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=closed)
    )
    assert _said(fake) == "Already charged 2 credits by <@U0STAFF>."


async def test_set_charge_when_out_of_range_then_names_the_range(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    await handle_settle_click(
        runtime, _settle_click(SETTLE_SET_ACTION_ID, escalation_id, message=post)
    )
    await run_settle_submission(runtime, evaluate_settle_submission(_charge(_opened(fake), "5")))
    assert _said(fake) == "Enter an amount from 1 to 2.", "capped by the balance"
    assert _sent(fake, _UPDATE) == [], "the post stays pending"
    ((_id, status, *_rest),) = await _rows(db_session_factory)
    assert status == "pending"


async def test_set_charge_when_not_an_amount_then_the_form_says_so() -> None:
    form = {
        "callback_id": SETTLE_CALLBACK_ID,
        "private_metadata": json.dumps(
            {"escalation_id": uuid.uuid4().hex, "channel_id": _ESC_CHANNEL, "message_ts": _POST_TS}
        ),
    }
    sent = evaluate_settle_submission(_charge(form, "lots"))
    assert not sent.proceed and sent.response_payload is not None
    assert list(sent.response_payload["errors"]) == ["support_charge_block"]


async def test_decline_when_pending_then_releases_the_hold(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    await handle_settle_click(
        runtime, _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=post)
    )
    assert _said(fake) == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}"
    assert "Declined by <@U0STAFF>" in _sent(fake, _UPDATE)[-1]["text"]
    ((_id, status, _hold, charge, _post, _answers, by),) = await _rows(db_session_factory)
    assert (status, charge, by) == ("declined", D(0), _STAFF)


@pytest.mark.parametrize(
    ("channel", "support"),
    [("C_OTHER", {}), (_ESC_CHANNEL, {"slack_escalation_team_id": "T_OTHER"})],
    ids=["other channel", "other workspace"],
)
async def test_settle_click_when_outside_the_support_channel_then_refuses(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake: Any,
    channel: str,
    support: dict[str, str],
) -> None:
    key, _revision = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory, **support)
    message = {"ts": _POST_TS}
    click = _settle_click(
        SETTLE_DECLINE_ACTION_ID, uuid.uuid4().hex, message=message, channel=channel
    )
    await handle_settle_click(runtime, click)
    assert _said(fake) == SETTLE_ELSEWHERE

"""Support limits on Slack: the menu, option forms, admin exemption and settling a post.

Real Postgres plus the transport-level fake Slack, as `test_support_escalation.py`.
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack import support_escalation as slack_support
from daimon.adapters.slack import support_settle
from daimon.adapters.slack.support_escalation import (
    ASK_HUMAN_ACTION_ID,
    SUPPORT_CALLBACK_ID,
    evaluate_menu_submission,
    evaluate_support_submission,
    handle_ask_human_click,
    handle_option_pick,
    run_support_submission,
)
from daimon.adapters.slack.support_menu_form import (
    MENU_CALLBACK_ID,
    OPTION_ACTION_ID,
    OPTION_BLOCK_ID,
)
from daimon.adapters.slack.support_post import (
    NOT_NOTIFIED_LINE,
    SETTLE_ACCEPT_ACTION_ID,
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
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import support as store
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.support_escalation import submit_request
from daimon.core.support import (
    CLIENT_NOT_NOTIFIED,
    EXEMPT_OFFER,
    NOT_ENOUGH_CREDITS,
    OPTIONS_CHANGED,
    SETTLE_ELSEWHERE,
    RequestChoice,
    SupportPlace,
    option_value,
    parse_menu,
)
from daimon.core.support_escalation import ALREADY_REQUESTED
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import mock_settings
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
_ESC_CHANNEL = str(CHAT_OK_PAYLOAD["channel"])  # where the fake says a post landed
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

MENU = parse_menu(
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
    limit: bool = True,
) -> str:
    """A Slack tenant with a workspace limit (unless `limit` is False), on MENU unless `menu` is; its key."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    async with factory() as session, session.begin():
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM)
        await upsert_slack_bot_token(
            session, team_id=_TEAM, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        if menu:
            row = await store.set_menu(
                session, tenant_id=tenant.id, key="support", spec=MENU, set_by_account_id=None
            )
            await store.set_menu_use(
                session,
                tenant_id=tenant.id,
                scope="workspace",
                scope_id="",
                menu_id=row.id,
                set_by_account_id=None,
            )
        if limit:
            await store.set_limit(
                session,
                tenant_id=tenant.id,
                scope="workspace",
                scope_id="",
                platform_user_id=None,
                shared=None,
                each_person=per_person,
                window="total",
                starts_at=None,
                ends_at=None,
                set_by_account_id=None,
            )
    return fernet_key


def _runtime(fernet_key: str, factory: async_sessionmaker[AsyncSession], **support: Any) -> Any:
    settings = mock_settings()
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
    """The last view opened: the loading modal a settle click shows first."""
    return _sent(fake, _VIEWS_OPEN)[-1]["view"]


def _form(fake: Any) -> dict[str, Any]:
    """The last view updated in place: the Set charge form."""
    return _sent(fake, _VIEWS_UPDATE)[-1]["view"]


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


def _value(key: str) -> str:
    """The radio value MENU shows for `key`."""
    option = MENU.option(key)
    assert option is not None, f"{key} is on the menu"
    return option_value(option)


async def _pick(
    runtime: Any, fake: Any, view: dict[str, Any], key: str, value: str | None = None
) -> dict[str, Any]:
    await handle_option_pick(
        runtime,
        {
            "type": "block_actions",
            "team": {"id": _TEAM},
            "user": {"id": _USER, "team_id": _TEAM},
            "view": {"id": "V_TEST", "private_metadata": view["private_metadata"]},
            "actions": [
                {"action_id": OPTION_ACTION_ID, "selected_option": {"value": value or _value(key)}}
            ],
        },
    )
    return _shown(fake)[-1]


def _picked(key: str) -> dict[str, Any]:
    return {OPTION_ACTION_ID: {"selected_option": {"value": _value(key)}}}


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
    sent = evaluate_menu_submission(_send(picked, {OPTION_BLOCK_ID: _picked("review")}))
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


async def test_click_when_limit_has_a_menu_then_shows_options_and_balance(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """The click shows the menu's options and the balance."""
    key = await _seed(db_session_factory)
    view = await _menu(_runtime(key, db_session_factory), fake)
    assert view["callback_id"] == MENU_CALLBACK_ID
    radio = next(b for b in view["blocks"] if b.get("block_id") == OPTION_BLOCK_ID)["element"]
    assert [o["value"] for o in radio["options"]] == [
        _value(k) for k in ("quick", "review", "call")
    ], "each value carries its option's terms"
    assert radio["options"][1]["description"]["text"] == "1-3 credits", "each option's price"
    assert "2 of 2 credits left" in json.dumps(view), "the balance sits under the menu"
    assert await _rows(db_session_factory) == [], "opening the menu spends nothing"


async def test_pick_when_option_has_steps_and_fields_then_its_form_shows_them(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    view = await _pick(runtime, fake, await _menu(runtime, fake), "call")
    ids = [b.get("block_id") for b in view["blocks"] if b["type"] == "input"]
    assert ids == [OPTION_BLOCK_ID, "support_step:call", "support_field:call:topic"] + [
        "support_field:call:when"
    ], "the pick, its step and its two fields"
    assert json.loads(view["private_metadata"])["option"] == _value("call")


async def test_pick_when_the_menu_changed_then_options_changed(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    shown = await _pick(runtime, fake, await _menu(runtime, fake), "call", "call:older")
    assert "callback_id" not in shown and OPTIONS_CHANGED in json.dumps(shown)


async def test_pick_when_balance_cannot_cover_it_then_send_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory, per_person=D("0.5"))
    runtime = _runtime(key, db_session_factory)
    view = await _pick(runtime, fake, await _menu(runtime, fake), "review")
    assert NOT_ENOUGH_CREDITS in json.dumps(view), "the menu says why"
    sent = evaluate_menu_submission(_send(view, {OPTION_BLOCK_ID: _picked("review")}))
    assert not sent.proceed
    assert sent.response_payload == {
        "response_action": "errors",
        "errors": {OPTION_BLOCK_ID: NOT_ENOUGH_CREDITS},
    }


async def _asked_before(
    factory: async_sessionmaker[AsyncSession], key: str, *, message_ts: str = _ANSWER_TS
) -> None:
    """The asker already asked with `key` on an answer, straight through the ledger."""
    option = MENU.option(key)
    assert option is not None, f"{key} is on the menu"
    async with factory() as session, session.begin():
        outcome = await submit_request(
            session,
            tenant_id=derive_tenant_uuid(platform="slack", workspace_id=_TEAM),
            account_id=None,
            platform="slack",
            platform_user_id=_USER,
            channel_id=_CHANNEL,
            message_id=message_ts,
            ma_session_id=None,
            place=SupportPlace(channel_id=_CHANNEL, agent_id=None),
            choice=RequestChoice.of(option),
            default_allowance=20,
            exempt=False,
        )
    assert outcome.status == "recorded", outcome.status


async def test_click_when_nothing_is_affordable_then_says_why_with_the_balance_and_no_radio(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory, per_person=D("0.5"))
    await _asked_before(db_session_factory, "quick", message_ts="1700000002.000100")
    shown = json.dumps(await _menu(_runtime(key, db_session_factory), fake))
    assert OPTION_BLOCK_ID not in shown, "no radio when nothing can be picked"
    assert NOT_ENOUGH_CREDITS in shown and "0 of 0.5 credits left." in shown, shown


async def test_click_when_already_asked_and_nothing_else_is_affordable_then_already_asked(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory, per_person=D("0.5"))
    await _asked_before(db_session_factory, "quick")
    shown = json.dumps(await _menu(_runtime(key, db_session_factory), fake))
    assert OPTION_BLOCK_ID not in shown, "no radio"
    assert json.dumps(ALREADY_REQUESTED)[1:-1] in shown, "asked comes before credits"


async def test_pick_when_already_asked_at_no_credits_then_already_asked_and_send_refused(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory, per_person=D("0.5"))
    runtime = _runtime(key, db_session_factory)
    menu = await _menu(runtime, fake)
    await _asked_before(db_session_factory, "quick")
    view = await _pick(runtime, fake, menu, "quick")
    assert json.dumps(ALREADY_REQUESTED)[1:-1] in json.dumps(view), "the menu says why"
    sent = evaluate_menu_submission(_send(view, {OPTION_BLOCK_ID: _picked("quick")}))
    assert sent.response_payload == {
        "response_action": "errors",
        "errors": {OPTION_BLOCK_ID: ALREADY_REQUESTED},
    }, "a Send on it is refused the same way"


async def test_send_when_option_has_no_fields_then_posts_pending_with_settle_buttons(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory)
    escalation_id, post = await _pending(_runtime(key, db_session_factory), fake)
    assert "Review: 1-3 credits\nPending: 1-3 credits\n1 of 2 credits left" in post["text"], (
        "a limit request's post names its terms, status and balance"
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


async def test_send_when_charged_to_the_default_then_post_shows_facts_and_settle_buttons(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """A range option with no limit: facts and settle controls, and no balance."""
    key = await _seed(db_session_factory, limit=False)
    escalation_id, post = await _pending(_runtime(key, db_session_factory), fake)
    assert "Review: 1-3 credits\nPending: 1-3 credits" in post["text"], "option and pending price"
    assert "credits left" not in post["text"], "no limit, so no balance"
    assert NOT_NOTIFIED_LINE in post["text"], "the settler is told the client is not notified"
    buttons = post["blocks"][-1]["elements"]
    assert [b["action_id"] for b in buttons] == [SETTLE_SET_ACTION_ID, SETTLE_DECLINE_ACTION_ID]
    assert {b["value"] for b in buttons} == {escalation_id}, "both act on the request"


async def test_decline_when_no_escalation_workspace_is_named_then_the_requests_own_workspace_settles(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory)
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
    key = await _seed(db_session_factory)
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
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    await _pending(runtime, fake)
    again = await _menu(runtime, fake)
    blocks = [b.get("block_id") for b in again.get("blocks", [])]
    assert OPTION_BLOCK_ID in blocks, "a menu asks once per option, not once per answer"


async def test_send_when_option_has_steps_then_held_until_accepted_at_its_price(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    view = await _pick(runtime, fake, await _menu(runtime, fake), "call")
    values = {
        OPTION_BLOCK_ID: _picked("call"),
        "support_step:call": {"support_step_input": {"selected_option": {"value": "1"}}},
        "support_field:call:topic": {"support_field_input": {"value": "Billing export"}},
    }
    await run_support_submission(runtime, evaluate_menu_submission(_send(view, values)))
    post = _sent(fake, _POST)[-1]
    assert "Call, 1 hour: 1 credit\nPending: 1 credit" in post["text"], "a step waits"
    assert "Topic: Billing export" in post["text"], "the answers are the note"
    buttons = [b["action_id"] for b in post["blocks"][-1]["elements"]]
    assert buttons == [SETTLE_ACCEPT_ACTION_ID, SETTLE_DECLINE_ACTION_ID], "Accept, no amount"
    ((escalation_id, status, hold, *_rest),) = await _rows(db_session_factory)
    assert (status, hold) == ("pending", D(1)), "holds its price"
    post = {**post, "ts": _POST_TS}
    await handle_settle_click(
        runtime, _settle_click(SETTLE_ACCEPT_ACTION_ID, escalation_id.hex, message=post)
    )
    assert _said(fake) == f"Charged 1 credit. {CLIENT_NOT_NOTIFIED}", "charged at its price"
    assert "Charged 1 credit by <@U0STAFF>" in _sent(fake, _UPDATE)[-1]["text"], "closed"


async def test_default_note_when_requester_is_admin_then_uses_no_credits(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = await _seed(db_session_factory, menu=False)
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


async def test_decline_when_the_clicker_is_a_workspace_admin_requester_then_settles_own_request(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit exempts a workspace admin, so settle lets them settle their own request."""
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    own = _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=post)
    own["user"] = {"id": _USER, "team_id": _TEAM}
    await handle_settle_click(runtime, own)
    ((_id, status, *_rest),) = await _rows(db_session_factory)
    assert status == "pending", "a plain requester settles nothing"
    monkeypatch.setattr(support_settle, "resolve_is_platform_admin", AsyncMock(return_value=True))
    await handle_settle_click(runtime, own)
    ((_id, status, *_rest),) = await _rows(db_session_factory)
    assert status == "declined", "a workspace admin settles their own request"


async def test_set_charge_when_in_range_then_charges_and_closes_the_post(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    fake.mock.get(_HISTORY_PATTERN, payload={"ok": True, "messages": [post]}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
    await handle_settle_click(
        runtime, _settle_click(SETTLE_SET_ACTION_ID, escalation_id, message=post)
    )
    assert "Checking" in json.dumps(_opened(fake)), "a loading modal opens before the slow work"
    form = _form(fake)
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
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    await handle_settle_click(
        runtime, _settle_click(SETTLE_SET_ACTION_ID, escalation_id, message=post)
    )
    await run_settle_submission(runtime, evaluate_settle_submission(_charge(_form(fake), "5")))
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
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    await handle_settle_click(
        runtime, _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=post)
    )
    assert _said(fake) == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}"
    assert "Declined by <@U0STAFF>" in _sent(fake, _UPDATE)[-1]["text"]
    ((_id, status, _hold, charge, _post, _answers, by),) = await _rows(db_session_factory)
    assert (status, charge, by) == ("declined", D(0), _STAFF)


async def test_set_charge_click_when_outside_the_support_channel_then_the_modal_says_so(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    click = _settle_click(SETTLE_SET_ACTION_ID, escalation_id, message=post, channel="C_OTHER")
    await handle_settle_click(runtime, click)
    assert "Checking" in json.dumps(_opened(fake)), "the loading modal opened first"
    assert SETTLE_ELSEWHERE in json.dumps(_form(fake)), "the refusal replaces it in the modal"


async def test_settle_click_when_outside_the_support_channel_then_refuses(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    click = _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=post, channel="C_OTHER")
    await handle_settle_click(runtime, click)
    assert _said(fake) == SETTLE_ELSEWHERE, "only the posted channel settles"
    ((_id, status, *_rest),) = await _rows(db_session_factory)
    assert status == "pending", "nothing was settled"


async def _own_channel(factory: async_sessionmaker[AsyncSession], channel_id: str) -> None:
    """The workspace picks its own request channel."""
    async with factory() as session, session.begin():
        await store.set_support_channel(
            session,
            tenant_id=derive_tenant_uuid(platform="slack", workspace_id=_TEAM),
            platform="slack",
            channel_id=channel_id,
            slack_team_id=_TEAM,
            set_by_account_id=None,
        )


async def _stamps(factory: async_sessionmaker[AsyncSession]) -> list[tuple[Any, ...]]:
    async with factory() as session:
        result = await session.execute(
            text("SELECT support_channel_id, support_post_id FROM support_escalations")
        )
        return [tuple(r) for r in result.all()]


async def test_send_when_the_workspace_has_its_own_channel_then_posts_and_stamps_there(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """A workspace's own channel takes its requests; the stamp is where Slack says it landed."""
    key = await _seed(db_session_factory)
    await _own_channel(db_session_factory, "C_OWN")
    await _pending(_runtime(key, db_session_factory), fake)
    assert _sent(fake, _POST)[-1]["channel"] == "C_OWN", "the workspace's own channel"
    assert await _stamps(db_session_factory) == [(_ESC_CHANNEL, _POST_TS)], (
        "the channel Slack says the post landed in is stamped"
    )


async def test_send_when_own_channels_are_off_then_posts_to_the_deployment_channel(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """With `support_own_channel` off a picked channel is ignored."""
    key = await _seed(db_session_factory)
    await _own_channel(db_session_factory, "C_OWN")
    runtime = _runtime(key, db_session_factory)
    runtime.settings.features["support_own_channel"] = "off"
    await _pending(runtime, fake)
    assert _sent(fake, _POST)[-1]["channel"] == _ESC_CHANNEL, "the deployment's channel"


async def test_settle_when_the_channel_changed_then_the_open_request_settles_where_posted(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """An open request settles where it was posted, not in the newly picked channel."""
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    escalation_id, post = await _pending(runtime, fake)
    await _own_channel(db_session_factory, "C_OWN")
    moved = _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=post, channel="C_OWN")
    await handle_settle_click(runtime, moved)
    assert _said(fake) == SETTLE_ELSEWHERE, "the new channel did not get it"
    await handle_settle_click(
        runtime, _settle_click(SETTLE_DECLINE_ACTION_ID, escalation_id, message=post)
    )
    assert _said(fake) == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}", "the posted one does"


async def test_settle_when_the_request_predates_the_stamp_then_the_default_channel_settles(
    db_session_factory: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """A request from before the stamp settles in the deployment channel only."""
    key = await _seed(db_session_factory)
    runtime = _runtime(key, db_session_factory)
    await _asked_before(db_session_factory, "review")
    ((escalation_id, *_rest),) = await _rows(db_session_factory)
    assert await _stamps(db_session_factory) == [(None, None)], "an old, unstamped request"
    post: dict[str, Any] = {"ts": _POST_TS, "text": "Human support requested", "blocks": []}
    value = uuid.UUID(str(escalation_id)).hex
    elsewhere = _settle_click(SETTLE_DECLINE_ACTION_ID, value, message=post, channel="C_OTHER")
    await handle_settle_click(runtime, elsewhere)
    assert _said(fake) == SETTLE_ELSEWHERE, "another channel refuses"
    await handle_settle_click(runtime, _settle_click(SETTLE_DECLINE_ACTION_ID, value, message=post))
    assert _said(fake) == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}", "the default does"

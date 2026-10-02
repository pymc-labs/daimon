"""Slack "Ask a human" (support_escalation.py) — real Postgres + transport-level fake Slack.

Covers the click (modal or refusal), the note submission (ledger row, escalation
post, confirmation), the disabled-when-unset rule, the shared credit copy,
double-submit idempotency, sealed origins, and the access refusals.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.support_escalation import (
    ASK_HUMAN_ACTION_ID,
    NOT_ALLOWED,
    SEALED_NOTE_HINT,
    SUPPORT_CALLBACK_ID,
    evaluate_support_submission,
    handle_ask_human_click,
    run_support_submission,
    slack_support_enabled,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import SupportSettings
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.support_escalation import (
    ALREADY_REQUESTED,
    OUT_OF_CREDITS,
    RECORDED_UNDELIVERED,
    UNAVAILABLE,
    offer_text,
    received_text,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import build_slack_runtime

pytestmark = pytest.mark.asyncio

_TEAM = "T_SUP"
_USER = "U_ASKER"
_CHANNEL = "C_ANSWERS"
_THREAD = "1700000000.000001"
_ANSWER_TS = "1700000001.000100"
_ESC_CHANNEL = "C_ESCALATE"
_PERMALINK = "https://sup.slack.com/archives/C_ANSWERS/p1700000001000100"

_EPHEMERAL = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
_POST = ("POST", yarl.URL("https://slack.com/api/chat.postMessage"))
_VIEWS_OPEN = ("POST", yarl.URL("https://slack.com/api/views.open"))
_PERMALINK_PATTERN = re.compile(r"https://slack\.com/api/chat\.getPermalink.*")


def _support(**overrides: Any) -> SupportSettings:
    values: dict[str, Any] = {"slack_escalation_channel_id": _ESC_CHANNEL, **overrides}
    return SupportSettings(**values)


async def _seed(
    session: AsyncSession, *, policy: TenantAccessPolicy | None = None
) -> tuple[uuid.UUID, str]:
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM)
    await upsert_slack_bot_token(
        session, team_id=_TEAM, encrypted_token=encrypt_token(fernet, "xoxb-test")
    )
    if policy is not None:
        await set_access_policy(session, tenant_id=tenant.id, policy=policy)
    await session.commit()
    return tenant.id, fernet_key


def _runtime(
    fernet_key: str, factory: async_sessionmaker[AsyncSession], support: SupportSettings
) -> Any:
    settings = MagicMock()
    settings.support = support
    return build_slack_runtime(fernet_key, factory, settings=settings)


def _click(*, user_team: str = _TEAM) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM},
        "user": {"id": _USER, "team_id": user_team, "username": "asker"},
        "channel": {"id": _CHANNEL},
        "container": {"message_ts": _ANSWER_TS, "channel_id": _CHANNEL},
        "message": {"ts": _ANSWER_TS, "thread_ts": _THREAD},
        "trigger_id": "TRIGGER",
        "actions": [{"action_id": ASK_HUMAN_ACTION_ID}],
    }


def _submit_payload(note: str, *, message_ts: str = _ANSWER_TS) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": _TEAM},
        "user": {"id": _USER, "team_id": _TEAM, "username": "asker"},
        "view": {
            "callback_id": SUPPORT_CALLBACK_ID,
            "private_metadata": json.dumps(
                {"channel_id": _CHANNEL, "message_ts": message_ts, "thread_ts": _THREAD}
            ),
            "state": {"values": {"support_note_block": {"support_note_input": {"value": note}}}},
        },
    }


def _ephemeral_texts(fake: Any) -> list[str]:
    return [c.kwargs["json"]["text"] for c in fake.mock.requests.get(_EPHEMERAL, [])]


def _posts(fake: Any) -> list[dict[str, Any]]:
    return [c.kwargs["json"] for c in fake.mock.requests.get(_POST, [])]


async def _rows(factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with factory() as s:
        result = await s.execute(
            text(
                "SELECT platform, platform_user_id, channel_id, message_id, note, delivered_at"
                " FROM support_escalations ORDER BY created_at"
            )
        )
        return list(result.mappings())


@pytest.fixture
def permalink(fake_slack_web_client: Any) -> Any:
    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        _PERMALINK_PATTERN, payload={"ok": True, "permalink": _PERMALINK}, repeat=True
    )
    return fake_slack_web_client


# ---------------------------------------------------------------------------
# enablement
# ---------------------------------------------------------------------------


async def test_slack_support_is_off_until_its_own_channel_is_set() -> None:
    assert not slack_support_enabled(SupportSettings())
    assert not slack_support_enabled(SupportSettings(escalation_channel_id="123")), (
        "the Discord channel must not switch Slack on: no cross-platform posting"
    )
    assert slack_support_enabled(_support())
    assert not slack_support_enabled(_support(credits_per_user=0))


async def test_click_when_unset_opens_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, SupportSettings())

    await handle_ask_human_click(runtime, _click())

    assert _VIEWS_OPEN not in fake_slack_web_client.mock.requests
    assert _ephemeral_texts(fake_slack_web_client) == [UNAVAILABLE]


async def test_submit_when_unset_records_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, SupportSettings())

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    assert await _rows(db_session_factory) == []
    assert _posts(fake_slack_web_client) == []


# ---------------------------------------------------------------------------
# click
# ---------------------------------------------------------------------------


async def test_click_opens_the_note_form_with_the_remaining_count(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())

    await handle_ask_human_click(runtime, _click())

    opens = fake_slack_web_client.mock.requests.get(_VIEWS_OPEN, [])
    assert len(opens) == 1
    view = opens[0].kwargs["json"]["view"]
    assert view["callback_id"] == SUPPORT_CALLBACK_ID
    assert view["blocks"][0]["text"]["text"] == offer_text(remaining=20), (
        "the default allowance is 20"
    )
    assert json.loads(view["private_metadata"]) == {
        "channel_id": _CHANNEL,
        "message_ts": _ANSWER_TS,
        "thread_ts": _THREAD,
    }
    assert SEALED_NOTE_HINT not in json.dumps(view)
    assert await _rows(db_session_factory) == [], "clicking must not spend a credit"


async def test_click_with_no_credits_left_says_so(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support(credits_per_user=1))
    await run_support_submission(
        runtime, evaluate_support_submission(_submit_payload("x", message_ts="1.1"))
    )

    await handle_ask_human_click(runtime, _click())

    assert _VIEWS_OPEN not in permalink.mock.requests
    assert _ephemeral_texts(permalink)[-1] == OUT_OF_CREDITS


async def test_click_by_someone_outside_the_invoker_allowlist_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session, policy=TenantAccessPolicy(invoker_user_ids=("U_OTHER",)))
    runtime = _runtime(key, db_session_factory, _support())

    await handle_ask_human_click(runtime, _click())

    assert _VIEWS_OPEN not in fake_slack_web_client.mock.requests
    assert _ephemeral_texts(fake_slack_web_client) == [NOT_ALLOWED]


async def test_click_in_a_protected_channel_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(
        db_session, policy=TenantAccessPolicy(protected_channel_ids=(_CHANNEL,))
    )
    runtime = _runtime(key, db_session_factory, _support())

    await handle_ask_human_click(runtime, _click())

    assert _VIEWS_OPEN not in fake_slack_web_client.mock.requests
    assert _ephemeral_texts(fake_slack_web_client) == [NOT_ALLOWED]


async def test_external_slack_connect_click_is_ignored(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())

    await handle_ask_human_click(runtime, _click(user_team="T_ELSEWHERE"))

    assert _VIEWS_OPEN not in fake_slack_web_client.mock.requests
    assert _ephemeral_texts(fake_slack_web_client) == []


async def test_click_after_asking_on_this_answer_says_it_is_in_hand(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())
    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    await handle_ask_human_click(runtime, _click())

    assert _VIEWS_OPEN not in permalink.mock.requests
    assert _ephemeral_texts(permalink)[-1] == ALREADY_REQUESTED


# ---------------------------------------------------------------------------
# submission
# ---------------------------------------------------------------------------


async def test_empty_note_is_bounced_back_to_the_form() -> None:
    decision = evaluate_support_submission(_submit_payload("   "))
    assert decision.proceed is False
    assert decision.response_payload is not None
    assert decision.response_payload["response_action"] == "errors"


async def test_external_submission_goes_no_further() -> None:
    payload = _submit_payload("help")
    payload["user"]["team_id"] = "T_ELSEWHERE"
    decision = evaluate_support_submission(payload)
    assert decision.proceed is False and decision.response_payload is None


async def test_submission_records_posts_and_confirms(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())

    await run_support_submission(
        runtime, evaluate_support_submission(_submit_payload("the <!channel> forecast is off"))
    )

    rows = await _rows(db_session_factory)
    assert len(rows) == 1
    assert rows[0]["platform"] == "slack" and rows[0]["message_id"] == _ANSWER_TS
    assert rows[0]["delivered_at"] is not None, "a landed post stamps delivered_at"
    posts = _posts(permalink)
    assert len(posts) == 1
    body = posts[0]
    assert body["channel"] == _ESC_CHANNEL
    assert _PERMALINK in body["text"]
    assert f"<@{_USER}>" in body["text"]
    assert "&lt;!channel&gt;" in body["text"], "the note must not be able to ping the channel"
    assert body["unfurl_links"] is False and body["unfurl_media"] is False, (
        "an unfurled permalink would copy the answer into the escalation channel"
    )
    assert _ephemeral_texts(permalink) == [received_text(remaining=19)]


async def test_double_submit_spends_one_credit_and_posts_once(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())
    decision = evaluate_support_submission(_submit_payload("help"))

    await run_support_submission(runtime, decision)
    await run_support_submission(runtime, decision)

    assert len(await _rows(db_session_factory)) == 1
    assert len(_posts(permalink)) == 1, "a retry must not post twice"
    assert _ephemeral_texts(permalink) == [received_text(remaining=19), ALREADY_REQUESTED]


async def test_out_of_credits_submission_records_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support(credits_per_user=1))

    await run_support_submission(
        runtime, evaluate_support_submission(_submit_payload("a", message_ts="1.1"))
    )
    await run_support_submission(
        runtime, evaluate_support_submission(_submit_payload("b", message_ts="1.2"))
    )

    assert len(await _rows(db_session_factory)) == 1
    assert len(_posts(permalink)) == 1
    assert _ephemeral_texts(permalink) == [received_text(remaining=0), OUT_OF_CREDITS]


async def test_submission_is_refused_when_access_was_revoked_after_the_click(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant_id, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())
    async with db_session_factory() as s, s.begin():
        await set_access_policy(
            s, tenant_id=tenant_id, policy=TenantAccessPolicy(invoker_user_ids=("U_OTHER",))
        )

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    assert await _rows(db_session_factory) == []
    assert _posts(permalink) == []
    assert _ephemeral_texts(permalink) == [NOT_ALLOWED]


async def test_sealed_origin_posts_a_link_and_the_note_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(
        db_session, policy=TenantAccessPolicy(sealed_channel_ids=(f"{_CHANNEL}:{_THREAD}",))
    )
    runtime = _runtime(key, db_session_factory, _support())

    await handle_ask_human_click(runtime, _click())
    view = permalink.mock.requests[_VIEWS_OPEN][0].kwargs["json"]["view"]
    assert SEALED_NOTE_HINT in json.dumps(view), "the person is told the note leaves the seal"

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help me")))

    (body,) = _posts(permalink)
    lines = body["text"].split("\n")
    assert lines[0].startswith("*Human support requested* by ")
    assert lines[1] == _PERMALINK
    assert "sealed" in lines[2]
    assert lines[3:] == ["", "help me"], "nothing but the requester, link and note"
    assert body["unfurl_links"] is False


async def test_protected_escalation_channel_keeps_the_row_undelivered(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(
        db_session, policy=TenantAccessPolicy(protected_channel_ids=(_ESC_CHANNEL,))
    )
    runtime = _runtime(key, db_session_factory, _support())

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    rows = await _rows(db_session_factory)
    assert len(rows) == 1 and rows[0]["delivered_at"] is None
    assert _posts(permalink) == []
    assert _ephemeral_texts(permalink) == [RECORDED_UNDELIVERED]


async def test_escalation_workspace_without_daimon_keeps_the_row_undelivered(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support(slack_escalation_team_id="T_OPS"))

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    rows = await _rows(db_session_factory)
    assert len(rows) == 1 and rows[0]["delivered_at"] is None
    assert _posts(permalink) == []
    assert _ephemeral_texts(permalink) == [RECORDED_UNDELIVERED]

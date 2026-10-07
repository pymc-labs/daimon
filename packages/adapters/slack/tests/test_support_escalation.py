"""Slack "Ask a human" (support_escalation.py) — real Postgres + transport-level fake Slack.

Covers the click (modal or refusal), the note submission (ledger row, escalation
post, confirmation), the disabled-when-unset rule, the shared credit copy,
double-submit idempotency, sealed origins, and the access refusals.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import yarl
from aioresponses import CallbackResult
from cryptography.fernet import Fernet
from daimon.adapters.slack import support_escalation as slack_support
from daimon.adapters.slack.support_escalation import (
    ASK_HUMAN_ACTION_ID,
    CHECK_FAILED,
    FORM_DID_NOT_OPEN,
    NOT_ALLOWED,
    SEALED_NOTE_HINT,
    SUPPORT_CALLBACK_ID,
    evaluate_support_submission,
    handle_ask_human_click,
    run_support_submission,
    slack_support_enabled,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_admins import GroupMembersCache
from daimon.core.config import DirectMessagePolicy, SupportSettings
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores import accounts
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.tenants import get_tenant
from daimon.core.support_escalation import (
    ALREADY_REQUESTED,
    OUT_OF_CREDITS,
    RECORDED_UNDELIVERED,
    UNAVAILABLE,
    offer_text,
    received_text,
)
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
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
_VIEWS_UPDATE = ("POST", yarl.URL("https://slack.com/api/views.update"))
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


def _shown(fake: Any) -> list[dict[str, Any]]:
    """Every view the click put in front of the person: the open, then each update."""
    opens = [c.kwargs["json"]["view"] for c in fake.mock.requests.get(_VIEWS_OPEN, [])]
    updates = [c.kwargs["json"]["view"] for c in fake.mock.requests.get(_VIEWS_UPDATE, [])]
    return opens + updates


def _notice(fake: Any) -> str:
    """The text of the last view shown, when it is a notice rather than a form."""
    last = _shown(fake)[-1]
    assert "callback_id" not in last, "expected a notice, got a form"
    return last["blocks"][0]["text"]["text"]


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
    assert "callback_id" not in opens[0].kwargs["json"]["view"], (
        "the click opens a Checking notice before running any check"
    )
    view = _shown(fake_slack_web_client)[-1]
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

    assert _notice(permalink) == OUT_OF_CREDITS, "the opened modal is replaced by the reason"
    assert all("callback_id" not in v for v in _shown(permalink)), "no form is offered"


async def test_click_by_someone_outside_the_invoker_allowlist_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session, policy=TenantAccessPolicy(invoker_user_ids=("U_OTHER",)))
    runtime = _runtime(key, db_session_factory, _support())

    await handle_ask_human_click(runtime, _click())

    assert _notice(fake_slack_web_client) == NOT_ALLOWED
    assert all("callback_id" not in v for v in _shown(fake_slack_web_client)), "no form"
    assert _ephemeral_texts(fake_slack_web_client) == []


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

    assert _notice(fake_slack_web_client) == NOT_ALLOWED
    assert all("callback_id" not in v for v in _shown(fake_slack_web_client)), "no form"
    assert _ephemeral_texts(fake_slack_web_client) == []


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

    assert _notice(permalink) == ALREADY_REQUESTED, "the opened modal is replaced by the reason"
    assert all("callback_id" not in v for v in _shown(permalink)), "no form is offered"


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
    view = _shown(permalink)[-1]
    assert SEALED_NOTE_HINT in json.dumps(view), "the person is told the note leaves the seal"

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help me")))

    (body,) = _posts(permalink)
    lines = body["text"].split("\n")
    assert lines[0].startswith("*Human support requested* by ")
    assert lines[1] == _PERMALINK
    assert "read only from inside" in lines[2]
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


# ---------------------------------------------------------------------------
# a channel with its own admins
# ---------------------------------------------------------------------------

_OPEN_DM_PATTERN = re.compile(r"https://slack\.com/api/conversations\.open.*")


async def _grant(session: AsyncSession, tenant_id: uuid.UUID, *, admins: tuple[str, ...]) -> None:
    tenant = await get_tenant(session, tenant_id)
    assert tenant is not None
    account = await make_account(session, tenant=tenant)
    await make_platform_principal(
        session, platform="slack", external_id="U_SERVER", tenant=tenant, account=account
    )
    await accounts.set_role(session, account.id, Role.ADMIN)
    await set_channel_admins(
        session,
        tenant_id=tenant_id,
        platform="slack",
        channel_id=_CHANNEL,
        role_ids=(),
        user_ids=admins,
        actor_account_id=None,
    )
    await session.commit()


def _dms(fake: Any, *, refuse: frozenset[str] = frozenset()) -> None:
    def opened(url: yarl.URL, **_: Any) -> CallbackResult:
        user = url.query["users"]
        if user in refuse:
            return CallbackResult(payload={"ok": False, "error": "cannot_dm_bot"})
        return CallbackResult(payload={"ok": True, "channel": {"id": f"D_{user}"}})

    fake.mock.post(_OPEN_DM_PATTERN, callback=opened, repeat=True)


async def test_a_channel_with_admins_sends_the_request_to_them_not_the_escalation_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant_id, key = await _seed(db_session)
    await _grant(db_session, tenant_id, admins=("U_LEAD", _USER))
    _dms(permalink)
    runtime = _runtime(key, db_session_factory, _support())
    runtime.settings.direct_message_policies = {}

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    (body,) = _posts(permalink)
    assert body["channel"] == "D_U_LEAD", "the channel's admin hears it, never the asker"
    assert _PERMALINK in body["text"] and body["text"].endswith("help")
    assert body["unfurl_links"] is False
    (row,) = await _rows(db_session_factory)
    assert row["delivered_at"] is not None
    assert _ephemeral_texts(permalink) == [received_text(remaining=19)]


async def test_unreachable_channel_admins_fall_back_to_server_admins_then_the_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant_id, key = await _seed(db_session)
    await _grant(db_session, tenant_id, admins=("U_LEAD",))
    _dms(permalink, refuse=frozenset({"U_LEAD"}))
    runtime = _runtime(key, db_session_factory, _support())
    runtime.settings.direct_message_policies = {}

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("one")))
    assert [p["channel"] for p in _posts(permalink)] == ["D_U_SERVER"]

    blocked = {tenant_id: DirectMessagePolicy(mode="disabled")}
    runtime.settings.direct_message_policies = blocked
    await run_support_submission(
        runtime, evaluate_support_submission(_submit_payload("two", message_ts="1700000002.0"))
    )
    assert [p["channel"] for p in _posts(permalink)] == ["D_U_SERVER", _ESC_CHANNEL], (
        "with no admin reachable, the escalation channel still gets it"
    )


_GROUP_USERS_PATTERN = re.compile(r"https://slack\.com/api/usergroups\.users\.list.*")


async def test_a_stored_group_is_looked_up_with_no_session_open(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    """A slow Slack must hold neither a pooled connection nor the ledger transaction."""
    tenant_id, key = await _seed(db_session)
    tenant = await get_tenant(db_session, tenant_id)
    assert tenant is not None
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="slack", external_id=_USER, tenant=tenant, account=account
    )
    await accounts.set_platform_role_ids(db_session, account.id, ["S1"])
    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        channel_id=_CHANNEL,
        role_ids=["S1"],
        user_ids=[],
        actor_account_id=None,
    )
    await db_session.commit()
    open_sessions = [0]
    seen: list[int] = []

    @asynccontextmanager
    async def counting() -> AsyncIterator[AsyncSession]:
        open_sessions[0] += 1
        try:
            async with db_session_factory() as session:
                yield session
        finally:
            open_sessions[0] -= 1

    def listed(url: yarl.URL, **_: Any) -> CallbackResult:
        seen.append(open_sessions[0])
        return CallbackResult(payload={"ok": True, "users": [_USER]})

    permalink.mock.get(_GROUP_USERS_PATTERN, callback=listed, repeat=True)
    _dms(permalink)
    runtime = dataclasses.replace(
        _runtime(key, cast(Any, counting), _support()), group_members=GroupMembersCache(ttl_s=0)
    )
    runtime.settings.direct_message_policies = {}

    await handle_ask_human_click(runtime, _click())
    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    assert len(seen) >= 2, "the click and the submit each looked the group up"
    assert set(seen) == {0}, "every lookup ran after its session closed"


# ---------------------------------------------------------------------------
# the modal Slack would not open or replace
# ---------------------------------------------------------------------------


async def _refused_open(client: Any, *, trigger_id: str, view: dict[str, Any]) -> str | None:
    return None


async def test_a_refusal_comes_as_an_ephemeral_when_no_modal_opened(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session, policy=TenantAccessPolicy(invoker_user_ids=("U_X",)))
    runtime = _runtime(key, db_session_factory, _support())

    with patch.object(slack_support, "open_modal", new=_refused_open):
        await handle_ask_human_click(runtime, _click())

    assert _ephemeral_texts(fake_slack_web_client) == [NOT_ALLOWED]


async def test_a_form_that_could_not_be_shown_says_to_click_again_and_spends_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())

    with patch.object(slack_support, "open_modal", new=_refused_open):
        await handle_ask_human_click(runtime, _click())

    assert _ephemeral_texts(fake_slack_web_client) == [FORM_DID_NOT_OPEN]
    assert await _rows(db_session_factory) == []


async def test_a_missing_tenant_closes_the_checking_notice_with_a_reason(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, key = await _seed(db_session)
    await db_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await db_session.commit()
    runtime = _runtime(key, db_session_factory, _support())

    await handle_ask_human_click(runtime, _click())

    assert _notice(fake_slack_web_client) == UNAVAILABLE, "no Checking… notice is left hanging"


async def test_a_top_level_answer_in_a_dm_offers_the_form_for_that_answer(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())
    click = _click()
    click["channel"] = {"id": "D_DM"}
    click["container"] = {"message_ts": "1.5", "channel_id": "D_DM"}
    click["message"] = {"ts": "1.5"}

    await handle_ask_human_click(runtime, click)

    form = _shown(fake_slack_web_client)[-1]
    assert form["callback_id"] == SUPPORT_CALLBACK_ID
    assert json.loads(form["private_metadata"]) == {
        "channel_id": "D_DM",
        "message_ts": "1.5",
        "thread_ts": "1.5",
    }


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("SELECT 1", {}, Exception("connection reset")),
        ConnectionRefusedError(111, "Connection refused"),
        TimeoutError(),
    ],
    ids=["sqlalchemy", "raw-connection-refused", "timeout"],
)
async def test_a_database_failure_during_the_checks_replaces_the_checking_notice(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    error: BaseException,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise error

    with patch.object(slack_support, "check_place_access", new=failing):
        await handle_ask_human_click(runtime, _click())

    assert _notice(fake_slack_web_client) == CHECK_FAILED
    assert await _rows(db_session_factory) == []


@pytest.mark.parametrize("method", ["chat_getPermalink", "chat_postMessage"])
async def test_a_transport_timeout_while_delivering_keeps_the_row_and_says_so(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
    method: str,
) -> None:
    """An ambiguous send is recorded undelivered and the person is told; never retried."""
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())
    real = getattr(AsyncWebClient, method)

    async def timing_out(self: AsyncWebClient, **kwargs: Any) -> Any:
        if method == "chat_postMessage" and kwargs.get("channel") != _ESC_CHANNEL:
            return await real(self, **kwargs)
        raise TimeoutError

    with patch.object(AsyncWebClient, method, new=timing_out):
        await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    rows = await _rows(db_session_factory)
    assert len(rows) == 1
    if method == "chat_postMessage":
        assert rows[0]["delivered_at"] is None
        assert _ephemeral_texts(permalink) == [RECORDED_UNDELIVERED]
    else:
        assert rows[0]["delivered_at"] is not None, "a missing permalink still posts the ids"
        (body,) = _posts(permalink)
        assert f"message {_ANSWER_TS} in channel {_CHANNEL}" in body["text"]


async def test_a_failure_stamping_a_delivered_request_still_says_it_was_received(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, _support())

    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError(111, "Connection refused")

    with patch.object(slack_support, "mark_delivered", new=failing):
        await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    assert len(_posts(permalink)) == 1, "it landed"
    assert _ephemeral_texts(permalink) == [received_text(remaining=19)]


def test_support_modal_note_input_stays_within_slack_input_limit() -> None:
    """Slack refuses the whole view (`invalid_arguments`) when an input's
    `max_length` is above 3,000, so the note form would never open."""
    view = slack_support.build_support_modal(
        channel_id="C1", message_ts="1.2", thread_ts="1.0", remaining=3, sealed=True
    )
    lengths = [
        b["element"]["max_length"]
        for b in view["blocks"]
        if b["type"] == "input" and b["element"]["type"] == "plain_text_input"
    ]
    assert lengths and all(1 <= n <= 3000 for n in lengths)

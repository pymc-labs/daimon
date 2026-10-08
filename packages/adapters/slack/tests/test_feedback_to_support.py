"""A submitted 👎 form also goes to the support channel, for a tenant that turned it on.

Covers `SupportSettings.feedback_to_support` on Slack:
  - off by default: the form stays in the database and says nothing about sharing;
  - on: the form says it is shared, and each submission posts once to the
    channel Ask a human uses, with the person, the agent, the reasons, the
    text and a link to the answer, never an unfurl;
  - an identical resubmission does not post again, a changed one does;
  - on for another tenant, or with no Slack support channel, posts nothing;
  - a refused submitter posts nothing; a sealed origin is marked.
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
from daimon.adapters.slack.feedback import (
    FEEDBACK_VOTE_DOWN,
    evaluate_feedback_text_submission,
    handle_feedback_vote,
    run_feedback_text_submission,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import SupportSettings
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant, make_thread_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import build_slack_runtime

pytestmark = pytest.mark.asyncio

_TEAM = "T_FBS"
_USER = "U_CRITIC"
_CHANNEL = "C_ANSWERS"
_THREAD = "1700000000.000001"
_ANSWER_TS = "1700000001.000100"
_SUPPORT_CHANNEL = "C_SUPPORT"
_PERMALINK = "https://fbs.slack.com/archives/C_ANSWERS/p1700000001000100"

_EPHEMERAL = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
_POST = ("POST", yarl.URL("https://slack.com/api/chat.postMessage"))
_VIEWS_OPEN = ("POST", yarl.URL("https://slack.com/api/views.open"))
_PERMALINK_PATTERN = re.compile(r"https://slack\.com/api/chat\.getPermalink.*")


async def _seed(
    session: AsyncSession, *, policy: TenantAccessPolicy | None = None
) -> tuple[Any, str]:
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM)
    await upsert_slack_bot_token(
        session, team_id=_TEAM, encrypted_token=encrypt_token(fernet, "xoxb-test")
    )
    if policy is not None:
        await set_access_policy(session, tenant_id=tenant.id, policy=policy)
    await session.commit()
    return tenant, fernet_key


def _runtime(
    fernet_key: str,
    factory: async_sessionmaker[AsyncSession],
    *,
    routed: dict[uuid.UUID, bool] | None = None,
    channel: str | None = _SUPPORT_CHANNEL,
) -> Any:
    settings = MagicMock()
    settings.support = SupportSettings(
        slack_escalation_channel_id=channel, feedback_to_support=routed or {}
    )
    return build_slack_runtime(fernet_key, factory, settings=settings)


def _payload(text_value: str, reasons: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": _TEAM},
        "user": {"id": _USER, "team_id": _TEAM, "username": "critic"},
        "view": {
            "callback_id": "feedback_text",
            "private_metadata": json.dumps(
                {"channel_id": _CHANNEL, "message_ts": _ANSWER_TS, "thread_ts": _THREAD}
            ),
            "state": {
                "values": {
                    "feedback_text_block": {"feedback_text_input": {"value": text_value}},
                    "feedback_reasons_block": {
                        "feedback_reasons_input": {
                            "selected_options": [{"value": r} for r in reasons]
                        }
                    },
                }
            },
        },
    }


async def _submit(runtime: Any, text_value: str, reasons: tuple[str, ...] = ()) -> None:
    decision = evaluate_feedback_text_submission(_payload(text_value, reasons))
    assert decision.proceed
    await run_feedback_text_submission(runtime, team_id=_TEAM, user_id=_USER, decision=decision)


def _posts(fake: Any) -> list[dict[str, Any]]:
    return [c.kwargs["json"] for c in fake.mock.requests.get(_POST, [])]


def _ephemeral_texts(fake: Any) -> list[str]:
    return [c.kwargs["json"]["text"] for c in fake.mock.requests.get(_EPHEMERAL, [])]


async def _feedback_rows(factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with factory() as s:
        result = await s.execute(text("SELECT vote, feedback_text FROM message_feedback"))
        return list(result.mappings())


@pytest.fixture
def permalink(fake_slack_web_client: Any) -> Any:
    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        _PERMALINK_PATTERN, payload={"ok": True, "permalink": _PERMALINK}, repeat=True
    )
    return fake_slack_web_client


def _vote_click() -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM},
        "user": {"id": _USER, "team_id": _TEAM},
        "channel": {"id": _CHANNEL},
        "container": {"message_ts": _ANSWER_TS, "channel_id": _CHANNEL},
        "message": {"ts": _ANSWER_TS, "thread_ts": _THREAD},
        "trigger_id": "TRIGGER",
        "actions": [{"action_id": FEEDBACK_VOTE_DOWN}],
    }


def _opened_views(fake: Any) -> list[dict[str, Any]]:
    return [c.kwargs["json"]["view"] for c in fake.mock.requests.get(_VIEWS_OPEN, [])]


async def test_off_by_default_the_form_stays_in_the_database(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory)

    await handle_feedback_vote(runtime, _vote_click())
    await _submit(runtime, "wrong numbers", ("inaccurate",))

    assert "support team" not in json.dumps(_opened_views(permalink))
    assert _posts(permalink) == []
    (row,) = await _feedback_rows(db_session_factory)
    assert row["feedback_text"] == "wrong numbers"


async def test_on_the_form_says_it_is_shared_and_posts_once_to_the_support_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant, key = await _seed(db_session)
    await make_thread_session(
        db_session,
        tenant=tenant,
        platform="slack",
        thread_id=_THREAD,
        ma_session_id="sesn_fbs",
        ma_agent_id="agent_fbs",
    )
    await db_session.commit()
    runtime = _runtime(key, db_session_factory, routed={tenant.id: True})

    await handle_feedback_vote(runtime, _vote_click())
    assert "also goes to the support team" in json.dumps(_opened_views(permalink))

    await _submit(runtime, "the <!channel> totals are *off*", ("inaccurate", "too_slow"))

    (body,) = _posts(permalink)
    assert body["channel"] == _SUPPORT_CHANNEL
    assert body["unfurl_links"] is False and body["unfurl_media"] is False
    lines = body["text"].split("\n")
    assert (
        lines[0] == f"*\N{THUMBS DOWN SIGN} Feedback* from <@{_USER}> (critic, {_USER} in {_TEAM})"
    )
    assert lines[1] == _PERMALINK
    assert lines[2] == "Agent `agent_fbs`, session `sesn_fbs`"
    assert lines[3] == "*Reasons:* Wrong or inaccurate, Too slow"
    assert lines[4] == ""
    assert "&lt;!channel&gt;" in lines[5], "the text must not be able to ping the channel"
    assert _ephemeral_texts(permalink)[-1] == "Thanks — your feedback has been recorded."


async def test_an_identical_resubmission_posts_nothing_and_a_changed_one_posts_again(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, routed={tenant.id: True})

    await _submit(runtime, "", ("incomplete",))
    await _submit(runtime, "", ("incomplete",))
    assert len(_posts(permalink)) == 1, "one post per submission, not per retry"

    await _submit(runtime, "it stopped halfway", ("incomplete",))
    posts = _posts(permalink)
    assert len(posts) == 2
    assert posts[0]["text"].endswith("*Reasons:* Incomplete or cut off")
    assert posts[1]["text"].endswith("it stopped halfway")


async def test_on_for_another_tenant_posts_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    _tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, routed={uuid.uuid4(): True})

    await handle_feedback_vote(runtime, _vote_click())
    await _submit(runtime, "wrong")

    assert "support team" not in json.dumps(_opened_views(permalink))
    assert _posts(permalink) == []


async def test_on_without_a_slack_support_channel_posts_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant, key = await _seed(db_session)
    runtime = _runtime(key, db_session_factory, routed={tenant.id: True}, channel=None)

    await handle_feedback_vote(runtime, _vote_click())
    await _submit(runtime, "wrong")

    assert "support team" not in json.dumps(_opened_views(permalink))
    assert _posts(permalink) == []


async def test_a_refused_submitter_posts_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant, key = await _seed(db_session, policy=TenantAccessPolicy(invoker_user_ids=("U_OTHER",)))
    runtime = _runtime(key, db_session_factory, routed={tenant.id: True})

    await _submit(runtime, "wrong")

    assert _posts(permalink) == []
    assert await _feedback_rows(db_session_factory) == []


async def test_a_sealed_origin_is_marked_in_the_post(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant, key = await _seed(
        db_session, policy=TenantAccessPolicy(sealed_channel_ids=(f"{_CHANNEL}:{_THREAD}",))
    )
    runtime = _runtime(key, db_session_factory, routed={tenant.id: True})

    await _submit(runtime, "", ("other",))

    (body,) = _posts(permalink)
    assert "read only from inside" in body["text"]
    assert body["text"].split("\n")[1] == _PERMALINK


async def test_a_protected_support_channel_posts_nothing_and_still_thanks(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    permalink: Any,
) -> None:
    tenant, key = await _seed(
        db_session, policy=TenantAccessPolicy(protected_channel_ids=(_SUPPORT_CHANNEL,))
    )
    runtime = _runtime(key, db_session_factory, routed={tenant.id: True})

    await _submit(runtime, "wrong")

    assert _posts(permalink) == []
    (row,) = await _feedback_rows(db_session_factory)
    assert row["feedback_text"] == "wrong"
    assert _ephemeral_texts(permalink) == ["Thanks — your feedback has been recorded."]

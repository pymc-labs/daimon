"""Scenario: a long Slack thread survives a forced session replacement.

Slack-only (W4c): a thread whose history is far longer than one
`conversations.replies` page must still replace cleanly when the
responder's model changes underneath it -- and the replay that decides what
to tell the successor must never read more of that history than an ordinary
turn ever would (`history_page_limit`). The transfer itself is exercised
through the real `daimon.core.workspace_transfer` path (no adapter code
reimplements it): the old session's event log carries one `user.message`
with no `agent.message`, so `is_worth_checkpointing` is False and the
transfer degrades straight to `TranscriptOnly` -- no billed checkpoint turn,
no bundle -- which is enough to prove the successor's first `events.send`
carries the framed `<previous_session>` transcript.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
from aioresponses import aioresponses as AioResponsesMock
from anthropic.types.beta.sessions.beta_managed_agents_text_block import BetaManagedAgentsTextBlock
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from cryptography.fernet import Fernet
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.context import DEFAULT_PAGE_LIMIT
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.config import SlackSettings
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.session_snapshot import desired_snapshot, fingerprint_identity, fingerprint_mutable
from daimon.core.stores import tenant_ledger
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    get_thread_session_by_id,
)
from daimon.core.turn.deps import build_turn_deps
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response, session_response
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AGENT_ID, ENV_ID, build_turn_router

_SLACK_API_BASE = "https://slack.com/api"
_OLD_MODEL_ID = "claude-haiku-4-5"
_OLD_SESSION_ID = "sess_long_thread_before"
_NEW_SESSION_ID = "sess_long_thread_after"


def _old_session_events() -> list[dict[str, object]]:
    """One `user.message`, no `agent.message`: enough for a transcript, not
    enough to be `is_worth_checkpointing` -- the transfer degrades straight
    to `TranscriptOnly` with no billed checkpoint turn and no bundle."""
    return [
        BetaManagedAgentsUserMessageEvent(
            id="evt_long_thread_user_msg",
            type="user.message",
            processed_at=datetime.now(UTC),
            content=[
                BetaManagedAgentsTextBlock(type="text", text="please finish the migration writeup")
            ],
        ).model_dump(mode="json")
    ]


def _build_router(tenant_id_str: str, *, sent_event_bodies: list[dict[str, Any]]) -> MARouter:
    """The shared turn router scoped to the successor session (its reply
    carries no usage event), plus the OLD session's event log and the
    successor's creation."""
    router = build_turn_router(
        tenant_id_str,
        session_id=_NEW_SESSION_ID,
        replacement_session_ids=(_OLD_SESSION_ID,),
        usage_event_id=None,
        sent_event_bodies=sent_event_bodies,
    )
    # The OLD session's event log: read once by `transfer_workspace` to
    # decide the transcript and the checkpoint-worth gate.
    router.add(
        "GET",
        rf"/v1/sessions/{_OLD_SESSION_ID}/events",
        lambda req, _m: list_response(_old_session_events()),
    )
    # A fresh successor session, created by the real `create_session` call
    # site (`daimon.core.turn.prepare.create_fresh_session`) -- not stubbed,
    # so the replacement decision itself runs for real.
    router.add(
        "POST",
        r"/v1/sessions",
        lambda req, _m: session_response(
            session_id=_NEW_SESSION_ID, agent_id=AGENT_ID, environment_id=ENV_ID
        ),
    )
    return router


def _register_slack_defaults(
    mock: AioResponsesMock, *, thread_messages: list[dict[str, Any]]
) -> None:
    import re

    users_info_pattern = re.compile(r"https://slack\.com/api/users\.info.*")
    reactions_add_pattern = re.compile(r"https://slack\.com/api/reactions\.add.*")
    conversations_replies_pattern = re.compile(r"https://slack\.com/api/conversations\.replies.*")

    mock.get(  # pyright: ignore[reportUnknownMemberType]
        users_info_pattern,
        payload={
            "ok": True,
            "user": {"is_admin": False, "is_owner": False, "is_primary_owner": False},
        },
        repeat=True,
    )
    # auth.test carries the bot's own user id, which the mention gate matches
    # against the event's <@U_BOT> token.
    mock.post(  # pyright: ignore[reportUnknownMemberType]
        f"{_SLACK_API_BASE}/auth.test",
        payload={"ok": True, "user_id": "U_BOT"},
        repeat=True,
    )
    for method in ("chat.postMessage", "chat.update", "chat.postEphemeral"):
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/{method}",
            payload={"ok": True, "ts": "1000000000.000001", "channel": "C_LONG_THREAD"},
            repeat=True,
        )
    mock.post(reactions_add_pattern, payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
    # The thread "conceptually" runs past one page; Slack itself would never
    # hand back more than one page -- this is that truncated page, exactly
    # what the real API would return for `limit=DEFAULT_PAGE_LIMIT`.
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        conversations_replies_pattern,
        payload={"ok": True, "messages": thread_messages, "has_more": True},
        repeat=True,
    )


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession], router: MARouter, *, fernet_key: str
) -> SlackRuntime:
    from daimon.testing.ma import build_fake_anthropic

    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.slack = SlackSettings(
        signing_secret=SecretStr("long-thread-signing-secret"),
        app_token=SecretStr("xapp-long-thread-test"),
        max_concurrent_turns_per_tenant=100,
    )
    settings.mcp.public_url = None
    settings.mcp.app_root_url = None
    settings.mcp.jwt_secret = None
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")
    anthropic = build_fake_anthropic(router.dispatch)
    deployment_default = DeploymentDefault(agent_name="test-agent", environment_name="test-env")
    resolver_cache = new_resolver_cache()
    turn_deps = build_turn_deps(
        settings,
        anthropic,
        sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        billing_config=None,
    )
    return SlackRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=resolver_cache,
        turn_deps=turn_deps,
        deployment_default=deployment_default,
    )


async def test_slack_long_thread_replacement_stays_within_thread_page_limit_and_carries_previous_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = "T_LONG_THREAD_PARITY"
    user_id = "U_LONG_THREAD_PARITY"
    channel_id = "C_LONG_THREAD_PARITY"
    thread_id = "9000007000.000001"

    tenant = await make_tenant(db_session, platform="slack", workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id=user_id
    )

    # The session this thread's previous turn ran on froze the OLD model;
    # the tenant's agent has since moved to MODEL_ID -- an identity-axis
    # mismatch that forces a replacement, not an in-place refresh.
    old_snapshot = desired_snapshot(
        ma_agent(id=AGENT_ID, model=_OLD_MODEL_ID),
        hidden_mcp_server_names=frozenset(),
        environment_id=ENV_ID,
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )
    old_row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        thread_id=thread_id,
        account_id=principal.account_id,
        ma_session_id=_OLD_SESSION_ID,
        ma_agent_id=AGENT_ID,
        watermark_message_id="9000007000.000000",
        effective_config=old_snapshot,
        identity_fingerprint=fingerprint_identity(old_snapshot),
        mutable_fingerprint=fingerprint_mutable(old_snapshot),
    )
    await db_session.commit()

    sent_event_bodies: list[dict[str, Any]] = []
    router = _build_router(str(tenant.id), sent_event_bodies=sent_event_bodies)

    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    async with db_session_factory() as s:
        await upsert_slack_bot_token(
            s, team_id=workspace_id, encrypted_token=encrypt_token(fernet, "xoxb-long-thread")
        )
        await s.commit()

    runtime = _make_runtime(db_session_factory, router, fernet_key=fernet_key)
    app = SlackApp(runtime=runtime)

    # The thread "conceptually" holds more human messages than one page; one
    # truncated page (DEFAULT_PAGE_LIMIT) is what a real Slack workspace would
    # ever hand back for a single `conversations.replies` call.
    thread_messages = [
        {
            "user": f"U_OTHER_{i}",
            "text": f"message number {i}",
            "ts": f"9000007000.{i:06d}",
        }
        for i in range(DEFAULT_PAGE_LIMIT)
    ]

    event_ts = f"{time.time():.6f}"
    event: dict[str, Any] = {
        "type": "app_mention",
        "channel": channel_id,
        "event_ts": event_ts,
        "ts": event_ts,
        "thread_ts": thread_id,
        "user": user_id,
        "text": "<@U_BOT> carry on with the migration",
    }

    with AioResponsesMock() as mock:
        _register_slack_defaults(mock, thread_messages=thread_messages)
        await app._handle_app_mention(event, team_id=workspace_id)  # pyright: ignore[reportPrivateUsage]

        replies_requests = [
            req
            for (method, url), reqs in mock.requests.items()
            if method == "GET" and str(url).startswith(f"{_SLACK_API_BASE}/conversations.replies")
            for req in reqs
        ]

    assert replies_requests, "the turn must replay the thread via conversations.replies"
    assert len(replies_requests) == 1, (
        "a single mention must fetch the thread history exactly once, never paginate for more"
    )
    replies_kwargs = cast(dict[str, Any], replies_requests[0].kwargs)  # pyright: ignore[reportUnknownMemberType]
    replies_params = cast(dict[str, Any], replies_kwargs.get("params") or {})
    assert int(replies_params["limit"]) == DEFAULT_PAGE_LIMIT, (
        "the request must ask for at most history_page_limit messages regardless of how long "
        "the real thread is"
    )

    # The old mapping is superseded by a new live row on the new session.
    old = await get_thread_session_by_id(db_session, id=old_row.id)
    assert old is not None and old.status == "superseded", (
        "the session frozen on the old model must hand its task on, not keep running"
    )
    live = await get_live_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        thread_id=thread_id,
        account_id=principal.account_id,
    )
    assert live is not None and live.id != old_row.id
    assert live.ma_session_id == _NEW_SESSION_ID
    assert live.predecessor_id == old_row.id
    assert live.transfer_kind == "transcript", (
        "no agent.message in the old log means is_worth_checkpointing is False -- the "
        "transfer must degrade straight to TranscriptOnly, never spend a checkpoint turn"
    )

    # The successor's FIRST events.send call must carry the framed previous-session
    # transcript -- proof the transfer's user_prefix actually reached the wire, not
    # just that the core transfer function returned one in isolation.
    assert sent_event_bodies, "the successor session must have sent at least one event batch"
    first_batch = sent_event_bodies[0]
    first_events = cast(list[dict[str, Any]], first_batch["events"])
    user_message_events = [e for e in first_events if e.get("type") == "user.message"]
    assert user_message_events, "the successor's first batch must include a user.message"
    first_user_text = "".join(
        str(block.get("text", ""))
        for block in cast(list[dict[str, Any]], user_message_events[0]["content"])
    )
    assert "<previous_session" in first_user_text, (
        "the successor's first outgoing message must carry the <previous_session> "
        "transcript from the old session's events"
    )
    assert "please finish the migration writeup" in first_user_text, (
        "the quoted transcript must be the old session's actual conversation, not a stub"
    )

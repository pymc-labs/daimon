"""Tests for the Slack credential-request click gate.

handle_credential_request_click (real Postgres + FakeSlackWebClient):
  - A live request clicked by its requester opens the kind's modal.
  - Wrong requester / expired / already-used / unknown token / wrong
    workspace each answer with an ephemeral and never open a modal, and a
    row that is both expired and used answers "expired" (Discord's order).
  - The repo kind refuses a non-admin whose agent cannot be resolved
    (fail closed) and lets a workspace admin through before any MA read.
  - A late click flips the card to expired; a bystander's click never edits it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from daimon.adapters.slack.credential_requests import (
    CRED_CALLBACK_PREFIX,
    handle_credential_request_click,
)
from daimon.core.credential_requests import (
    ENV_FILE_TARGET,
)
from daimon.core.posted_controls import (
    ALREADY_USED_MESSAGE,
    NO_LONGER_VALID_MESSAGE,
    WRONG_REQUESTER_MESSAGE,
    expired_message,
)
from daimon.core.stores.credential_requests import (
    consume_credential_request,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .credential_helpers import (
    _CHANNEL_ID,
    _CHAT_UPDATE_URL,
    _EPHEMERAL_URL,
    _MESSAGE_TS,
    _TEAM_ID,
    _USER_ID,
    _VIEWS_OPEN_URL,
    _build_runtime,
    _chat_updates,
    _ephemeral_texts,
    _seed_request,
    _seed_team,
)


def _click_payload(token: str, *, user_id: str = _USER_ID) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": _TEAM_ID},
        "user": {"id": user_id},
        "channel": {"id": _CHANNEL_ID},
        "container": {"message_ts": _MESSAGE_TS},
        "message": {"ts": _MESSAGE_TS},
        "trigger_id": "TRIGGER_TEST",
        "actions": [{"action_id": "credential_request", "value": token}],
    }


@pytest.mark.asyncio
async def test_live_request_clicked_by_requester_opens_the_kind_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, kind="env")
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    opens = fake_slack_web_client.mock.requests.get(("POST", _VIEWS_OPEN_URL), [])
    assert len(opens) == 1, "a live request clicked by its requester must open the modal"
    view = opens[0].kwargs["json"]["view"]
    assert view["callback_id"] == f"{CRED_CALLBACK_PREFIX}env"
    for block in [b for b in view["blocks"] if b["type"] == "input"]:
        assert 1 <= block["element"]["max_length"] <= 3000, (
            "Slack rejects plain-text inputs whose maximum length exceeds 3000"
        )
    assert "tester" in json.dumps(view["blocks"]), (
        "the form states the agent the request named, from the row"
    )
    meta = json.loads(view["private_metadata"])
    assert meta["token"] == token


@pytest.mark.asyncio
async def test_wrong_requester_gets_ephemeral_and_no_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, requester="U_SOMEONE_ELSE")
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert _ephemeral_texts(fake_slack_web_client) == [WRONG_REQUESTER_MESSAGE], (
        "the click refusal is the shared copy, character for character"
    )


@pytest.mark.asyncio
async def test_env_file_request_click_opens_the_upload_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session, tenant_id=tenant_id, kind="env_file", target=ENV_FILE_TARGET
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    opens = fake_slack_web_client.mock.requests.get(("POST", _VIEWS_OPEN_URL), [])
    assert len(opens) == 1, "an env_file request opens a form like every other kind"
    view = opens[0].kwargs["json"]["view"]
    assert view["callback_id"] == f"{CRED_CALLBACK_PREFIX}env_file"
    elements = [b["element"]["type"] for b in view["blocks"] if b["type"] == "input"]
    assert elements == ["file_input"], "the env_file form asks for an upload"


@pytest.mark.asyncio
async def test_expired_request_gets_ephemeral_and_no_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, expires_in=timedelta(minutes=-1))
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert _ephemeral_texts(fake_slack_web_client) == [
        expired_message(
            kind="env",
            agent_name="tester",
            responder_name="Daimon",
            target="OPENAI_API_KEY",
        )
    ], "an expired click says exactly what the expired card beside it says"


@pytest.mark.asyncio
async def test_unknown_token_gets_ephemeral_and_no_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload("never-minted"))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert _ephemeral_texts(fake_slack_web_client) == [NO_LONGER_VALID_MESSAGE], (
        "the click refusal is the shared copy, character for character"
    )


@pytest.mark.asyncio
async def test_already_used_request_gets_ephemeral_and_no_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id)
    consumed = await consume_credential_request(db_session, token=token, now=datetime.now(UTC))
    assert consumed is not None
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert _ephemeral_texts(fake_slack_web_client) == [ALREADY_USED_MESSAGE], (
        "the click refusal is the shared copy, character for character"
    )


@pytest.mark.asyncio
async def test_expired_request_answers_expired_even_when_also_used(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Pins the check order against Discord's `interaction_check`: a row that
    is both expired and used answers "expired", not "already used"."""
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, expires_in=timedelta(minutes=-1))
    await db_session.execute(
        text("UPDATE credential_requests SET used_at = now() WHERE token = :token"),
        {"token": token},
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    texts = _ephemeral_texts(fake_slack_web_client)
    assert any("expired" in t for t in texts)
    assert not any("already used" in t for t in texts)


@pytest.mark.asyncio
async def test_cross_workspace_click_gets_ephemeral_and_no_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A token minted for one workspace clicked from another must refuse even
    for the right requester — the tenant check, not message routing, is what
    keeps a leaked token unusable elsewhere."""
    _tenant_id, fernet_key = await _seed_team(db_session)
    other = await make_tenant(db_session, platform="slack", workspace_id="T_ELSEWHERE")
    token = await _seed_request(db_session, tenant_id=other.id)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert any("isn't for this workspace" in t for t in _ephemeral_texts(fake_slack_web_client))


@pytest.mark.asyncio
async def test_repo_kind_refuses_non_admin_when_agent_cannot_be_resolved(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Fail closed: the row's derived agent uuid resolving to nothing means
    the agent was archived or deleted since the mint — a non-admin must not
    reach the modal."""
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session, tenant_id=tenant_id, kind="repo", target="https://github.com/o/r"
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await handle_credential_request_click(runtime, _click_payload(token))

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests


async def test_expired_click_flips_the_card(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Nothing sweeps expired requests, so the first late click is the only
    chance to stop the card advertising a form that can no longer open."""
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session, tenant_id=tenant_id, kind="env", expires_in=timedelta(minutes=-5)
    )
    await db_session.commit()

    await handle_credential_request_click(
        _build_runtime(fernet_key, db_session_factory), _click_payload(token)
    )

    assert ("POST", _VIEWS_OPEN_URL) not in fake_slack_web_client.mock.requests, (
        "an expired request must never open a form"
    )
    card = _chat_updates(fake_slack_web_client)[-1]
    assert card["text"] == "⌛ This form expired.", "the card must stop offering the form"
    assert not [b for b in card["blocks"] if b["type"] == "actions"], (
        "the expired card must not keep a live button"
    )


async def test_wrong_requester_click_never_edits_the_card(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(db_session, tenant_id=tenant_id, kind="env")
    await db_session.commit()

    await handle_credential_request_click(
        _build_runtime(fernet_key, db_session_factory),
        _click_payload(token, user_id="U_SOMEONE_ELSE"),
    )

    assert ("POST", _CHAT_UPDATE_URL) not in fake_slack_web_client.mock.requests, (
        "a bystander's click must not be able to change what the requester's card says"
    )


@pytest.mark.asyncio
async def test_mcp_oauth_click_sends_a_private_sign_in_link_instead_of_a_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Same contract as Discord: the request is spent, a flow is minted, and the
    start link rides an ephemeral only the requester sees."""
    from daimon.core.github_credentials import build_multifernet
    from daimon.core.stores import mcp_oauth_flows as flows_store
    from daimon.core.stores.credential_requests import peek_credential_request

    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="mcp_oauth",
        target="notion",
        mcp_server_url="https://mcp.notion.com/mcp",
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory, mcp_configured=True)
    runtime.settings.mcp.app_root_url = "https://d.example"
    runtime.turn_deps.fernet = build_multifernet((fernet_key,))

    await handle_credential_request_click(runtime, _click_payload(token))

    opens = fake_slack_web_client.mock.requests.get(("POST", _VIEWS_OPEN_URL), [])
    assert opens == [], "an OAuth request never opens a modal"
    posts = fake_slack_web_client.mock.requests.get(("POST", _EPHEMERAL_URL), [])
    assert len(posts) == 1, "the requester gets exactly one ephemeral"
    payload = posts[0].kwargs["json"]
    button = payload["blocks"][1]["elements"][0]
    assert button["url"].startswith("https://d.example/oauth/mcp/start?state="), (
        "the ephemeral carries the start link as a url button"
    )
    state = button["url"].rsplit("state=", 1)[1]
    async with db_session_factory() as session:
        flow = await flows_store.get_flow(session, state=state)
        spent = await peek_credential_request(session, token=token)
    assert flow is not None and flow.request_token == token
    assert spent is not None and spent.used_at is not None, "the request is spent on the click"


@pytest.mark.asyncio
async def test_mcp_oauth_click_refuses_when_the_deployment_has_no_crypto_keys(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Same predicate as the route mount: no crypto keys, no link, request kept."""
    from daimon.core.stores.credential_requests import peek_credential_request

    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="mcp_oauth",
        target="notion",
        mcp_server_url="https://mcp.notion.com/mcp",
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory, mcp_configured=True)
    runtime.settings.mcp.app_root_url = "https://d.example"
    runtime.turn_deps.fernet = None

    await handle_credential_request_click(runtime, _click_payload(token))

    posts = fake_slack_web_client.mock.requests.get(("POST", _EPHEMERAL_URL), [])
    assert len(posts) == 1 and "cannot sign you in" in posts[0].kwargs["json"]["text"], (
        "the person learns the operator must finish setup"
    )
    async with db_session_factory() as session:
        spent = await peek_credential_request(session, token=token)
    assert spent is not None and spent.used_at is None, "the request survives for a later click"

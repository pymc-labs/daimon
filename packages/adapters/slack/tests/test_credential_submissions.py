"""Tests for the Slack credential-request submissions (`credential_submissions.py`).

Behavioral assertions — grouped by runner:
  - env: consume + agent_files write commit together; the button message is
    updated in place and the requester gets an ephemeral.
  - env_file: an unreadable file leaves the request unspent and the card
    untouched; a key that already exists refuses the whole import and writes
    nothing; a good file writes every key atomically and the card becomes the
    only receipt.
  - A second submission of a consumed row writes nothing.
  - mcp: missing daimon-mcp configuration refuses BEFORE the consume; every
    failure after it leaves the card in a terminal state rather than
    "Received. Saving…".
  - repo: non-admin refused before the consume; an admin binds a public repo.
  - skill_repo: the pasted token binds the repo and the imported skills are
    attached to the agent the request named; a member is refused before the
    consume on a shared agent, and a defaults-managed agent is never attached.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.adapters.slack import credential_submissions as credential_submissions_mod
from daimon.adapters.slack.agent_policy import SHARED_AGENT_SKILLS_MESSAGE
from daimon.adapters.slack.credential_requests import (
    run_env_credential_submission,
    run_env_file_credential_submission,
    run_mcp_credential_submission,
    run_repo_bind_credential_submission,
    run_skill_repo_credential_submission,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core import credential_submit
from daimon.core.credential_requests import (
    ENV_FILE_TARGET,
    build_skill_repo_target,
    mint_request_token,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.env_file import MAX_ENV_FILE_BYTES
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import build_multifernet, get_pat
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.posted_controls import RECEIVED_FOOTER
from daimon.core.scope import TenantScopeRef
from daimon.core.stores import scoped_config_write
from daimon.core.stores.agent_files import get_agent_file, put_agent_file
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.agent_skill_repo_credentials import get_skill_repo_credential
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.seeded_skills import record_seeded_skill
from daimon.core.stores.task_continuations import list_pending_continuations
from daimon.testing import list_response, ma_agent
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .credential_helpers import (
    _CHANNEL_ID,
    _CHAT_UPDATE_URL,
    _EPHEMERAL_URL,
    _MESSAGE_TS,
    _TEAM_ID,
    _USER_ID,
    _agents_handler,
    _build_runtime,
    _chat_updates,
    _ephemeral_texts,
    _noop_dispatch,
    _override_users_info_admin,
    _recording_trigger,
    _seed_request,
    _seed_team,
)

# ---------------------------------------------------------------------------
# run_env_credential_submission
# ---------------------------------------------------------------------------


async def _agent_file_rows(session: AsyncSession) -> list[Any]:
    result = await session.execute(
        text("SELECT tenant_id, agent_id, key FROM agent_files ORDER BY key")
    )
    rows = []
    for tenant_id, agent_id, key in result:
        row = await get_agent_file(session, tenant_id=tenant_id, agent_id=agent_id, key=key)
        assert row is not None
        rows.append({"key": row.key, "content": row.content})
    return rows


@pytest.mark.asyncio
async def test_env_submission_consumes_row_and_writes_the_secret(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)

    def ma_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([live_agent.model_dump(mode="json")])
        raise AssertionError(f"Unexpected MA request: {request.method} {request.url.path}")

    token = await _seed_request(
        db_session,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        tenant_id=tenant_id,
        kind="env",
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=ma_handler)

    await run_env_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="s3cr3t-value",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert rows and rows[0]["key"] == "OPENAI_API_KEY"
    assert rows[0]["content"] == "s3cr3t-value"
    assert row is not None and row.used_at is not None, "the consume must have committed"
    edit = fake_slack_web_client.mock.requests[("POST", _CHAT_UPDATE_URL)][0].kwargs["json"]
    assert edit["text"] == "🔑 Add OPENAI_API_KEY to tester", (
        "the consumed card keeps its headline instead of collapsing to a marker"
    )
    assert not [b for b in edit["blocks"] if b["type"] == "actions"], (
        "the consumed card must not keep a live button"
    )
    assert ("POST", _EPHEMERAL_URL) not in fake_slack_web_client.mock.requests, (
        "the card is the receipt on a success path — an ephemeral beside it would "
        "announce the same save twice in the same conversation"
    )


@pytest.mark.asyncio
async def test_env_submission_of_consumed_row_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)

    def ma_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([live_agent.model_dump(mode="json")])
        raise AssertionError(f"Unexpected MA request: {request.method} {request.url.path}")

    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=ma_handler)

    common: dict[str, Any] = {
        "team_id": _TEAM_ID,
        "user_id": _USER_ID,
        "channel_id": _CHANNEL_ID,
        "message_ts": _MESSAGE_TS,
        "token": token,
        "dispatch_continuations": _noop_dispatch,
    }
    await run_env_credential_submission(runtime, value="first", **common)
    await run_env_credential_submission(runtime, value="second", **common)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
    assert len(rows) == 1 and rows[0]["content"] == "first", (
        "a consumed row must never produce a second write"
    )


# ---------------------------------------------------------------------------
# run_env_file_credential_submission
# ---------------------------------------------------------------------------

_FILE_ID = "F_ENV_UPLOAD"
_DOWNLOAD_URL = "https://files.slack.com/files-pri/T_CRED-F_ENV_UPLOAD/download/.env"


def _patch_file_download(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    """Serve `files.info` and the private download URL at the HTTP boundary."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer xoxb-test", (
            "a private Slack file is only readable with the workspace's own bot token"
        )
        if request.url.path == "/api/files.info":
            assert request.url.params["file"] == _FILE_ID, (
                "the runner must fetch the file the submission named"
            )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "file": {
                        "id": _FILE_ID,
                        "name": ".env",
                        "mimetype": "text/plain",
                        "size": len(body),
                        "url_private_download": _DOWNLOAD_URL,
                    },
                },
            )
        assert str(request.url) == _DOWNLOAD_URL, f"unexpected download url: {request.url}"
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        credential_submissions_mod,
        "_download_client",
        lambda: httpx.AsyncClient(transport=transport),
    )


async def _seed_env_file_request(
    db_session: AsyncSession, *, tenant_id: uuid.UUID, live_agent: Any
) -> str:
    return await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env_file",
        target=ENV_FILE_TARGET,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
    )


async def _run_env_file_submission(runtime: SlackRuntime, token: str) -> None:
    await run_env_file_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        file_id=_FILE_ID,
        dispatch_continuations=_noop_dispatch,
    )


@pytest.mark.asyncio
async def test_env_file_submission_writes_every_key_and_makes_the_card_the_receipt(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await db_session.commit()
    _patch_file_download(
        monkeypatch, b"# keys\nALPHA_KEY=alpha-private\nexport BETA_KEY='beta-private'\n"
    )
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert [(r["key"], r["content"]) for r in rows] == [
        ("ALPHA_KEY", "alpha-private"),
        ("BETA_KEY", "beta-private"),
    ], "every key the file declared is stored, parsed by the documented grammar"
    assert row is not None and row.used_at is not None, "the consume commits with the writes"
    assert row.outcome == "applied", "the row records how the import ended"
    edits = _chat_updates(fake_slack_web_client)
    assert len(edits) == 1, "the card goes straight to its final state"
    assert edits[0]["text"] == "✅ 2 keys saved for tester.", "the card counts what it saved"
    assert "alpha-private" not in json.dumps(edits[0]), "no value ever reaches a posted message"
    assert ("POST", _EPHEMERAL_URL) not in fake_slack_web_client.mock.requests, (
        "the card is the receipt; a second one would say the same thing twice"
    )


@pytest.mark.asyncio
async def test_env_file_submission_refuses_a_non_credential_name_for_a_member(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A member upload may carry only credential names; a tool-control name is refused."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await db_session.commit()
    _patch_file_download(monkeypatch, b"API_KEY=ok\nLD_PRELOAD=/tmp/x.so\n")
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert rows == [], "a rejected import writes nothing"
    assert row is not None and row.used_at is None, "a rejected file must not spend the one click"


@pytest.mark.asyncio
async def test_env_file_submission_lets_an_admin_add_a_non_credential_name(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """An admin may upload a connection string; the hard-deny layer still applies to both."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await db_session.commit()
    _override_users_info_admin(fake_slack_web_client.mock)
    _patch_file_download(monkeypatch, b"API_KEY=ok\nDATABASE_URL=postgres://x\n")
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
    assert {r["key"] for r in rows} == {"API_KEY", "DATABASE_URL"}, (
        "an admin may add a non-credential name; the file still parses as before"
    )


@pytest.mark.asyncio
async def test_env_file_submission_refuses_the_whole_import_when_a_key_already_exists(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The card promised these keys were new. A file that would replace one is
    refused whole — the other keys in it are not written either."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        key="ALPHA_KEY",
        content="already-here",
        set_by_account_id=None,
    )
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await db_session.commit()
    _patch_file_download(monkeypatch, b"BETA_KEY=beta-private\nALPHA_KEY=alpha-private\n")
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert [(r["key"], r["content"]) for r in rows] == [("ALPHA_KEY", "already-here")], (
        "a collision writes nothing at all, not even the keys that would have been new"
    )
    assert row is not None and row.used_at is not None, "the click is spent either way"
    assert row.outcome == "stale_replacement", "the row records why nothing was written"
    edits = _chat_updates(fake_slack_web_client)
    assert len(edits) == 1, "the card is edited once, to the refusal"
    assert edits[0]["text"] == "🛡️ No keys were saved for tester.", (
        "the refused card says what did not happen"
    )
    rendered = json.dumps(edits[0]) + " ".join(_ephemeral_texts(fake_slack_web_client))
    assert "line 2: ALPHA_KEY is already set." in rendered, (
        "the refusal names the key and the line it came from"
    )
    assert "alpha-private" not in rendered and "beta-private" not in rendered, (
        "a refusal names lines and keys, never values"
    )
    assert _ephemeral_texts(fake_slack_web_client), "the person who submitted is told directly too"


@pytest.mark.asyncio
async def test_env_file_submission_of_an_unparsable_file_leaves_the_request_live(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await db_session.commit()
    _patch_file_download(monkeypatch, b"NOT A KEY LINE\n")
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert not rows, "a rejected file writes nothing"
    assert row is not None and row.used_at is None, (
        "a file we could not read must not spend the request — the corrected one reuses it"
    )
    assert ("POST", _CHAT_UPDATE_URL) not in fake_slack_web_client.mock.requests, (
        "the card stays in requested, button and all"
    )
    texts = _ephemeral_texts(fake_slack_web_client)
    assert texts and texts[0].startswith("No keys were saved for tester."), (
        "the person is told nothing changed"
    )
    assert "line 1:" in texts[0], "the rejection names the line that could not be read"


@pytest.mark.asyncio
async def test_env_file_submission_refuses_a_file_that_is_oversized_after_download(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The size checked before the download is the submitting client's claim.
    The bytes are what count, and they are measured here."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await db_session.commit()
    _patch_file_download(monkeypatch, b"BIG=" + b"x" * MAX_ENV_FILE_BYTES + b"\n")
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert not rows, "an oversized file is never parsed, so nothing is written"
    assert row is not None and row.used_at is None, "an unread file does not spend the request"
    assert ("POST", _CHAT_UPDATE_URL) not in fake_slack_web_client.mock.requests
    assert any("too big" in text for text in _ephemeral_texts(fake_slack_web_client)), (
        "the person is told the file was too big, not just that it failed"
    )


# ---------------------------------------------------------------------------
# run_mcp_credential_submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_submission_with_unconfigured_mcp_refuses_before_the_consume(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)

    def ma_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([live_agent.model_dump(mode="json")])
        raise AssertionError(f"Unexpected MA request: {request.method} {request.url.path}")

    token = await _seed_request(
        db_session,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        tenant_id=tenant_id,
        kind="mcp",
        target="my-server",
        mcp_server_url="https://mcp.example.com",
    )
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=ma_handler
    )  # mcp settings are None

    await run_mcp_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="mcp-token",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
    assert row is not None and row.used_at is None, (
        "a config refusal must land before the consume so the request survives"
    )
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests
    receipt = " ".join(_ephemeral_texts(fake_slack_web_client))
    assert "Ask the operator" in receipt and "Nothing was saved" in receipt, (
        "an unconfigured deployment should give an operator handoff and truthful save status"
    )
    assert "public_url" not in receipt and "jwt_secret" not in receipt, (
        "the person should not receive internal deployment setting names"
    )


# ---------------------------------------------------------------------------
# run_repo_bind_credential_submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_submission_refuses_non_admin_before_the_consume(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    token = await _seed_request(
        db_session, tenant_id=tenant_id, kind="repo", target="https://github.com/o/r"
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory)

    await run_repo_bind_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
    assert row is not None and row.used_at is None, (
        "the admin gate is the authorization boundary and must precede the consume"
    )
    assert ("POST", _EPHEMERAL_URL) in fake_slack_web_client.mock.requests
    assert ("POST", _CHAT_UPDATE_URL) not in fake_slack_web_client.mock.requests


@pytest.mark.asyncio
async def test_repo_submission_by_admin_binds_a_public_repo(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A workspace admin (per users.info) passes the gate and binds the repo."""
    monkeypatch.setattr(credential_submissions_mod, "is_public_repo", AsyncMock(return_value=True))
    _override_users_info_admin(fake_slack_web_client.mock)
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)

    def ma_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([live_agent.model_dump(mode="json")])
        raise AssertionError(f"Unexpected MA request: {request.method} {request.url.path}")

    token = await _seed_request(
        db_session,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        tenant_id=tenant_id,
        kind="repo",
        target=build_skill_repo_target("https://github.com/o/r", "release", ""),
    )
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=ma_handler)

    await run_repo_bind_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
        assert row is not None and row.used_at is not None
        binding = await get_binding(s, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert binding is not None and binding.ma_secret_ref == "anon:"
    assert binding.proof_kind == "public"
    assert binding.repo_url == "o/r", (
        "the repo URL is unpacked from the target before it is stored — a packed "
        "target would store the branch as part of the repo name"
    )
    assert binding.default_branch == "release", (
        "the branch comes from the target the card was posted for, not from the form"
    )
    assert ("POST", _CHAT_UPDATE_URL) in fake_slack_web_client.mock.requests


# ---------------------------------------------------------------------------
# run_skill_repo_credential_submission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_repo_submission_writes_the_skill_credential_not_the_working_repo_binding(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Importing puts skills in the tenant library; the request names an agent,
    so the submission must also attach them — import-without-attach leaves the
    user with an agent that has no skills and a success message saying
    otherwise. The pasted token lands in the skill-repo credential store and
    NOWHERE else: somebody offering a token so an agent can read skills out of
    a repo has not asked for that repo to become the agent's checkout, and the
    card they clicked said the working repo does not change."""
    monkeypatch.setattr(
        credential_submissions_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        credential_submissions_mod,
        "run_skill_sync",
        AsyncMock(
            return_value=[
                ResourceOutcome(
                    kind="skill",
                    name="imported-skill",
                    action=Action.CREATED,
                    anthropic_id="skill_01imported",
                )
            ]
        ),
    )
    tenant_id, fernet_key = await _seed_team(db_session)
    ma_agent_id = "agent_slack_skill_attach"
    assert tenant_id == derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="skill_repo",
        target=build_skill_repo_target("https://github.com/o/attach-repo", "main", ""),
        agent_id=agent_id,
    )
    await db_session.commit()

    agent = ma_agent(id=ma_agent_id, name="daimon", tenant_id=tenant_id)
    updates: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(agent.id):
            # Both the version-retry re-fetch and the update itself address the
            # agent directly and must parse as ONE agent; only the list route
            # gets the list envelope.
            if request.method in ("POST", "PATCH"):
                updates.append(json.loads(request.content))
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        return list_response([agent.model_dump(mode="json")])

    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    await run_skill_repo_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="ghp_slack_attach_token",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        binding = await get_binding(s, tenant_id=tenant_id, agent_id=agent_id)
        credential = await get_skill_repo_credential(
            s,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="https://github.com/o/attach-repo",
        )
    assert binding is None, "a skill-repo token must not become the agent's working repo binding"
    assert credential is not None, (
        "the skill-repo credential store is where a later sync looks for this token"
    )
    assert credential.proof_kind == "pat"
    assert (credential.default_branch, credential.path) == ("main", ""), (
        "branch and path come from the target the card was posted for"
    )
    assert updates, "the submission must call agents.update to attach the imported skills"
    attached_ids = {entry["skill_id"] for entry in updates[-1]["skills"]}
    assert "skill_01imported" in attached_ids, (
        "the newly imported skill must be attached to the agent the request named"
    )
    assert ("POST", _EPHEMERAL_URL) not in fake_slack_web_client.mock.requests, (
        "the card is the receipt on a success path"
    )
    assert _chat_updates(fake_slack_web_client)[-1]["text"] == (
        "✅ 1 skill added to tester from o/attach-repo."
    ), "the applied card must report the import through the shared change-confirmation copy"


def _imported_skill_sync() -> AsyncMock:
    return AsyncMock(
        return_value=[
            ResourceOutcome(
                kind="skill",
                name="imported-skill",
                action=Action.CREATED,
                anthropic_id="skill_01imported",
            )
        ]
    )


@pytest.mark.asyncio
async def test_skill_repo_submission_refuses_a_member_on_a_managed_agent_before_the_consume(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Importing attaches skills, so a member may not do it on a shared agent."""
    sync = _imported_skill_sync()
    monkeypatch.setattr(credential_submissions_mod, "run_skill_sync", sync)
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(
        id="agent_seeded",
        name="daimon",
        tenant_id=tenant_id,
        metadata={MA_METADATA_KEY_MANAGED: "true"},
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="skill_repo",
        agent_id=agent_id,
        target=build_skill_repo_target("https://github.com/o/skills", "main", ""),
    )
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await run_skill_repo_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="ghp_member_token",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
        credential = await get_skill_repo_credential(
            s, tenant_id=tenant_id, agent_id=agent_id, repo_url="https://github.com/o/skills"
        )
    assert row is not None and row.used_at is None, "the gate must precede the consume"
    assert credential is None, "a refused submission stores nothing"
    sync.assert_not_called()
    assert _ephemeral_texts(fake_slack_web_client) == [SHARED_AGENT_SKILLS_MESSAGE]


@pytest.mark.asyncio
async def test_skill_repo_submission_never_attaches_to_a_managed_agent_even_for_an_admin(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """An attach would drift the seeded spec for good, so the skills stay in the library."""
    monkeypatch.setattr(
        credential_submissions_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(credential_submissions_mod, "run_skill_sync", _imported_skill_sync())
    _override_users_info_admin(fake_slack_web_client.mock)
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(
        id="agent_seeded",
        name="daimon",
        tenant_id=tenant_id,
        metadata={MA_METADATA_KEY_MANAGED: "true"},
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="skill_repo",
        agent_id=agent_id,
        target=build_skill_repo_target("https://github.com/o/skills", "main", ""),
    )
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await run_skill_repo_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="ghp_admin_token",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
    assert row is not None and row.used_at is None and row.outcome == "write_failed"
    # `_agents_handler` fails the test on any agent update, so reaching here
    # means the managed agent was left untouched.
    card = json.dumps(_chat_updates(fake_slack_web_client)[-1])
    assert "but not added to tester" in card, "the card must not claim the agent has them"
    assert "is a built-in agent" in card, "the card says why"


async def _submit_skill_repo(
    runtime: SlackRuntime, token: str, value: str = "ghp_skill_token"
) -> None:
    await run_skill_repo_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value=value,
        dispatch_continuations=_noop_dispatch,
    )


@pytest.mark.asyncio
async def test_skill_repo_submission_refuses_a_member_on_a_reachable_unmanaged_agent(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Not built-in, but it answers for the workspace, so a member still needs an admin."""
    sync = _imported_skill_sync()
    monkeypatch.setattr(credential_submissions_mod, "run_skill_sync", sync)
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_shared", name="shared", tenant_id=tenant_id)
    await scoped_config_write.set_fields(
        db_session,
        scope=TenantScopeRef(tenant_id=tenant_id),
        tenant_id=tenant_id,
        agent_name="shared",
    )
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="skill_repo",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        target=build_skill_repo_target("https://github.com/o/skills", "main", ""),
    )
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _submit_skill_repo(runtime, token)

    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
    assert row is not None and row.used_at is None, "the gate must precede the consume"
    sync.assert_not_called()
    assert _ephemeral_texts(fake_slack_web_client) == [SHARED_AGENT_SKILLS_MESSAGE]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [False, True])
async def test_skill_repo_submission_passes_seeded_names_and_admin_status_to_the_sync(
    is_admin: bool,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The sync can only refuse seeded names and member replacements it is told about."""
    monkeypatch.setattr(
        credential_submissions_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    sync = AsyncMock(return_value=[])
    monkeypatch.setattr(credential_submissions_mod, "run_skill_sync", sync)
    if is_admin:
        _override_users_info_admin(fake_slack_web_client.mock)
    tenant_id, fernet_key = await _seed_team(db_session)
    await record_seeded_skill(
        db_session, tenant_id=tenant_id, name="eda", content_hash="h", anthropic_id="sk_eda"
    )
    live_agent = ma_agent(id="agent_private", name="private", tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="skill_repo",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        target=build_skill_repo_target("https://github.com/o/skills", "main", ""),
    )
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _submit_skill_repo(runtime, token)

    assert sync.call_args.kwargs["seeded_skill_names"] == frozenset({"eda"})
    assert sync.call_args.kwargs["is_admin"] is is_admin


@pytest.mark.asyncio
async def test_skill_repo_submission_puts_a_refused_import_on_the_card(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A refusal is not a success: nothing attaches, and the card says why."""
    monkeypatch.setattr(
        credential_submissions_mod, "pat_can_access_repo", AsyncMock(return_value=True)
    )
    reason = "skill name 'eda' belongs to a default skill. Rename the skill and re-sync."
    monkeypatch.setattr(
        credential_submissions_mod,
        "run_skill_sync",
        AsyncMock(
            return_value=[
                ResourceOutcome(
                    kind="skill", name="eda", action=Action.FAILED, error="raw", refusal=reason
                )
            ]
        ),
    )
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_private", name="private", tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="skill_repo",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        target=build_skill_repo_target("https://github.com/o/skills", "main", ""),
    )
    await db_session.commit()
    # `_agents_handler` fails the test on any agent update.
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _submit_skill_repo(runtime, token)

    card = json.dumps(_chat_updates(fake_slack_web_client)[-1])
    assert "The skills did not import." in card and reason in card
    assert "added to" not in card and "again to retry" not in card
    assert ("POST", _EPHEMERAL_URL) not in fake_slack_web_client.mock.requests, (
        "the card is the receipt"
    )


@pytest.mark.parametrize("fails_after_storage", [False, True])
async def test_skill_repo_failure_receipt_reflects_confirmed_token_storage(
    fails_after_storage: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)

    def ma_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([live_agent.model_dump(mode="json")])
        raise AssertionError(f"Unexpected MA request: {request.method} {request.url.path}")

    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="skill_repo",
        agent_id=agent_id,
        target=build_skill_repo_target("https://github.com/o/skills", "main", ""),
    )
    await db_session.commit()
    checks = 0

    def github_response(request: httpx.Request) -> httpx.Response:
        nonlocal checks
        checks += 1
        if checks == 1 or (checks == 2 and fails_after_storage):
            return httpx.Response(200, json={})
        return httpx.Response(403, text="sensitive-upstream-detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(github_response)) as http_client:
        runtime = replace(
            _build_runtime(fernet_key, db_session_factory, anthropic_handler=ma_handler),
            http_client=http_client,
        )
        await run_skill_repo_credential_submission(
            runtime,
            team_id=_TEAM_ID,
            user_id=_USER_ID,
            channel_id=_CHANNEL_ID,
            message_ts=_MESSAGE_TS,
            token=token,
            value="ghp_test_private_value",
            dispatch_continuations=_noop_dispatch,
        )
    stored = await get_pat(
        principal_id=derive_guild_account_uuid(tenant_id=tenant_id),
        agent_id=agent_id,
        sessionmaker=db_session_factory,
        fernet=build_multifernet((fernet_key,)),
    )
    receipt = " ".join(_ephemeral_texts(fake_slack_web_client))
    assert (stored is not None) == fails_after_storage, (
        "fixture must fail at the intended write stage"
    )
    assert ("Token stored" in receipt) == fails_after_storage, (
        "receipt must report only confirmed storage"
    )
    assert "Try again using the same form" in receipt, "a reachable retry"
    assert "sensitive-upstream-detail" not in receipt, (
        "upstream details must stay out of the receipt"
    )
    assert "ghp_test_private_value" not in receipt, "the private token must stay out of the receipt"


@pytest.mark.parametrize("wrong_dimension", ["requester", "tenant", "platform", "deleted_target"])
async def test_env_submission_rechecks_request_identity_before_consuming(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    wrong_dimension: str,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    request_tenant_id = tenant_id
    if wrong_dimension == "tenant":
        other_tenant = await make_tenant(db_session, platform="slack", workspace_id="T_OTHER")
        await make_account(
            db_session, tenant=other_tenant, id=derive_guild_account_uuid(tenant_id=other_tenant.id)
        )
        request_tenant_id = other_tenant.id
    token = mint_request_token()
    await create_credential_request(
        db_session,
        token=token,
        tenant_id=request_tenant_id,
        account_id=derive_guild_account_uuid(tenant_id=request_tenant_id),
        agent_id=uuid.uuid4(),
        kind="env",
        mcp_server_url=None,
        target="TOKEN",
        requester_platform_user_id="U_OTHER" if wrong_dimension == "requester" else _USER_ID,
        channel_id=_CHANNEL_ID,
        platform="discord" if wrong_dimension == "platform" else "slack",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="tester",
        requested_work=None,
    )
    await db_session.commit()
    await run_env_credential_submission(
        _build_runtime(fernet_key, db_session_factory),
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="private-value",
        dispatch_continuations=_noop_dispatch,
    )
    async with db_session_factory() as session:
        row = await peek_credential_request(session, token=token)
        files = await _agent_file_rows(session)
    assert row is not None and row.used_at is None, "wrong identity must not consume request"
    assert not files, "wrong identity must not write private input"


async def test_env_submission_uses_durable_destination_after_origin_turn_ends(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)

    def ma_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([live_agent.model_dump(mode="json")])
        raise AssertionError(f"Unexpected MA request: {request.method} {request.url.path}")

    token = mint_request_token()
    await create_credential_request(
        db_session,
        token=token,
        tenant_id=tenant_id,
        account_id=derive_guild_account_uuid(tenant_id=tenant_id),
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        kind="env",
        mcp_server_url=None,
        target="TOKEN",
        requester_platform_user_id=_USER_ID,
        channel_id="C_ORIGIN",
        platform="slack",
        parent_channel_id="C_ORIGIN",
        origin_thread_id="123.456",
        posted_message_id="123.789",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="tester",
        requested_work=None,
    )
    await db_session.commit()
    await run_env_credential_submission(
        _build_runtime(fernet_key, db_session_factory, anthropic_handler=ma_handler),
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id="C_REDIRECT",
        message_ts="999.999",
        token=token,
        value="private-value",
        dispatch_continuations=_noop_dispatch,
    )
    edit = _chat_updates(fake_slack_web_client)[-1]
    assert (edit["channel"], edit["ts"]) == ("C_ORIGIN", "123.789"), (
        "card identity must come from durable request"
    )
    assert ("POST", _EPHEMERAL_URL) not in fake_slack_web_client.mock.requests, (
        "the card carries the receipt, so nothing is said in the channel the form came from"
    )
    assert "private-value" not in json.dumps(edit), "the card must not disclose private input"
    async with db_session_factory() as s:
        pending = await list_pending_continuations(
            s, tenant_id=tenant_id, platform="slack", thread_id="123.456"
        )
    assert [row.parent_channel_id for row in pending] == ["C_ORIGIN"], (
        "the queued turn is addressed to the thread the request was minted in, "
        "not to wherever the form happened to be submitted from"
    )


# ---------------------------------------------------------------------------
# Truthful card states, continuations, and the dispatch trigger
# ---------------------------------------------------------------------------

_ORIGIN_THREAD = "1700000001.000100"
_WORK = "pull last week's Toggl hours"


async def _pending_continuations(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    thread_id: str = _ORIGIN_THREAD,
) -> list[Any]:
    async with db_session_factory() as session:
        return await list_pending_continuations(
            session, tenant_id=tenant_id, platform="slack", thread_id=thread_id
        )


async def _run_env(
    runtime: SlackRuntime, token: str, *, value: str = "s3cr3t", trigger: Any = None
) -> None:
    await run_env_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value=value,
        dispatch_continuations=trigger if trigger is not None else _noop_dispatch,
    )


async def test_env_submission_records_a_private_input_continuation_in_the_same_transaction(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        origin_thread_id=_ORIGIN_THREAD,
        requested_work=_WORK,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()
    seen: list[int] = []

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
        trigger=_recording_trigger(seen, client=fake_slack_web_client),
    )

    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
    pending = await _pending_continuations(db_session_factory, tenant_id=tenant_id)
    assert request_row is not None and request_row.outcome == "applied", (
        "the request must be recorded as applied once the key is stored"
    )
    assert len(pending) == 1, "a saved value with work waiting on it owes exactly one turn"
    queued = pending[0]
    assert queued.reason == "private_input_applied", (
        "the queued turn must be attributed to the private input, not to a handoff"
    )
    assert queued.idempotency_key == request_row.idempotency_key, (
        "the request's own key is what stops a retried submission queueing a second turn"
    )
    assert queued.requested_work == _WORK, "the work the person was promised must survive"
    assert queued.target_ma_agent_id == live_agent.id, (
        "the continuation is addressed to the agent the request froze, never to a name"
    )
    assert seen == [2], "the trigger runs after the card is edited to received and then applied"


async def test_env_submission_records_none_requested_work_for_a_save_only_request(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        origin_thread_id=_ORIGIN_THREAD,
        requested_work=None,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
    )

    pending = await _pending_continuations(db_session_factory, tenant_id=tenant_id)
    assert [row.requested_work for row in pending] == [None], (
        "a save-only request records the click and promises no turn"
    )
    card = json.dumps(_chat_updates(fake_slack_web_client)[-1])
    assert "OPENAI_API_KEY saved for tester." in card, "the card must confirm the save"
    assert "from your next message here" not in card, (
        "nothing was waiting on this key, so the card must not promise a next message"
    )


async def test_stale_replacement_leaves_the_stored_value_alone_and_renders_superseded(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        key="OPENAI_API_KEY",
        content="someone-elses-value",
        set_by_account_id=None,
    )
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        agent_id=agent_id,
        origin_thread_id=_ORIGIN_THREAD,
        requested_work=_WORK,
        target_ma_agent_id=live_agent.id,
        # The card promised to replace a value last written an hour ago; the
        # row in the table has moved on since.
        replaces_updated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    await db_session.commit()

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
        value="my-replacement",
    )

    async with db_session_factory() as session:
        rows = await _agent_file_rows(session)
        request_row = await peek_credential_request(session, token=token)
    assert [row["content"] for row in rows] == ["someone-elses-value"], (
        "a failed precondition must leave the stored value exactly as it was"
    )
    assert request_row is not None and request_row.outcome == "stale_replacement", (
        "the spent request must record why it wrote nothing"
    )
    assert not await _pending_continuations(db_session_factory, tenant_id=tenant_id), (
        "nothing was saved, so no turn may be queued on it"
    )
    card = _chat_updates(fake_slack_web_client)[-1]
    assert card["text"] == "⚠️ OPENAI_API_KEY was not replaced for tester.", (
        "the card must say the replacement did not happen"
    )
    assert "The current value is unchanged." in json.dumps(card)
    assert any(
        "was not replaced for tester" in text for text in _ephemeral_texts(fake_slack_web_client)
    ), "the submitter is told, in the card's own words, that a fresh request is needed"


async def test_replacement_needs_admin_at_submit_renders_refused_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The role is re-decided at submit: a shared agent's key is not a private
    contribution, and the form sat open long enough for the answer to change."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(
        id="agent_credentials",
        name="specialist",
        tenant_id=tenant_id,
        metadata={MA_METADATA_KEY_MANAGED: "true"},
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        key="OPENAI_API_KEY",
        content="shared-value",
        set_by_account_id=None,
    )
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        agent_id=agent_id,
        origin_thread_id=_ORIGIN_THREAD,
        requested_work=_WORK,
        target_ma_agent_id=live_agent.id,
        replaces_updated_at=datetime.now(UTC),
    )
    await db_session.commit()

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
        value="my-replacement",
    )

    async with db_session_factory() as session:
        rows = await _agent_file_rows(session)
        request_row = await peek_credential_request(session, token=token)
    assert [row["content"] for row in rows] == ["shared-value"], (
        "a refused replacement must not write"
    )
    assert request_row is not None and request_row.outcome == "write_failed", (
        "the refusal is part of the request's durable trace"
    )
    assert not await _pending_continuations(db_session_factory, tenant_id=tenant_id), (
        "a refused write promises no turn"
    )
    assert _chat_updates(fake_slack_web_client)[-1]["text"] == (
        "🛡️ OPENAI_API_KEY was not replaced for tester."
    ), "the card must carry the refusal, not just the ephemeral"


async def test_dispatch_trigger_runs_after_the_card_edit_and_its_failure_is_swallowed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        origin_thread_id=_ORIGIN_THREAD,
        requested_work=_WORK,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()
    seen: list[int] = []

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
        trigger=_recording_trigger(
            seen, client=fake_slack_web_client, raises=DaimonError("thread is busy")
        ),
    )

    assert seen == [2], "the receipt is on the card before any turn is dispatched"
    assert _chat_updates(fake_slack_web_client)[-1]["text"] == (
        "✅ OPENAI_API_KEY saved for tester."
    ), "a dispatch that cannot start must not unsay a save that already committed"
    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
    assert request_row is not None and request_row.outcome == "applied", (
        "the save is durable regardless of what the dispatch does"
    )


# ---------------------------------------------------------------------------
# mcp: the attach half decides applied vs partial
# ---------------------------------------------------------------------------

_MCP_VAULT_ID = "vlt_slack_cred"
_MCP_SERVER_URL = "https://ext.example.com/mcp"


def _mcp_handler(
    live_agent: Any,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    attach_fails: bool = False,
    vault_fails: bool = False,
    agent_gone_after_write: bool = False,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the MA routes one mcp submission walks: agent, vault, credentials.

    `attach_fails` refuses the agent update the attach half needs — the token
    is stored and the server is still unreachable, which is the partial state
    the card has to tell the truth about. `vault_fails` refuses the credential
    write itself, so nothing is stored at all. `agent_gone_after_write` lets
    the agent survive the pre-consume identity check and vanish before the
    attach looks it up — the only window in which that branch is real.
    """
    display_name = f"daimon-mcp:{account_id}:{agent_id}"
    credential_written = False
    # The agent keeps what was attached, so the publish step's re-read sees it.
    current = {"agent": live_agent.model_dump(mode="json")}
    lookups: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal credential_written
        path = request.url.path
        if request.method == "GET" and path == "/v1/agents":
            lookups.append(path)
            if agent_gone_after_write and len(lookups) > 2:
                # Survives the pre-consume identity and replacement checks,
                # gone by the time the connect looks it up.
                return list_response([])
            return list_response([current["agent"]])
        if path == f"/v1/agents/{live_agent.id}":
            if request.method in ("POST", "PATCH") and attach_fails:
                return httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "nope"},
                    },
                )
            if request.method in ("POST", "PATCH"):
                body = json.loads(request.content)
                current["agent"] = {**current["agent"], "mcp_servers": body.get("mcp_servers")}
            return httpx.Response(200, json=current["agent"])
        if request.method == "GET" and path == "/v1/vaults":
            return list_response(
                [
                    {
                        "id": _MCP_VAULT_ID,
                        "type": "vault",
                        "display_name": display_name,
                        "metadata": None,
                        "archived_at": None,
                        "created_at": "2026-04-01T00:00:00Z",
                    }
                ]
            )
        if path == f"/v1/vaults/{_MCP_VAULT_ID}/credentials":
            if request.method == "POST":
                if vault_fails:
                    return httpx.Response(
                        500,
                        json={
                            "type": "error",
                            "error": {"type": "api_error", "message": "vault unavailable"},
                        },
                    )
                credential_written = True
                body = json.loads(request.content)
                return httpx.Response(
                    200,
                    json={
                        "id": "vcrd_slack",
                        "type": "credential",
                        "vault_id": _MCP_VAULT_ID,
                        "auth": {
                            "type": "static_bearer",
                            "mcp_server_url": body["auth"]["mcp_server_url"],
                        },
                    },
                )
            return list_response([])
        raise AssertionError(f"Unexpected MA request: {request.method} {path}")

    return handler


async def _run_mcp_submission(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    attach_fails: bool = False,
    vault_fails: bool = False,
    agent_gone_after_write: bool = False,
    mcp_server_url: str | None = _MCP_SERVER_URL,
    posted_message_id: str | None = _MESSAGE_TS,
) -> tuple[uuid.UUID, str]:
    """Seed a live mcp request and run it; returns `(tenant_id, token)`."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    account_id = derive_guild_account_uuid(tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="mcp",
        target="my-server",
        mcp_server_url=mcp_server_url,
        agent_id=agent_id,
        posted_message_id=posted_message_id,
        origin_thread_id=_ORIGIN_THREAD,
        requested_work=_WORK,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        anthropic_handler=_mcp_handler(
            live_agent,
            account_id=account_id,
            agent_id=agent_id,
            attach_fails=attach_fails,
            vault_fails=vault_fails,
            agent_gone_after_write=agent_gone_after_write,
        ),
        mcp_configured=True,
    )
    runtime.turn_deps.fernet = build_multifernet((fernet_key,))
    await run_mcp_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="mcp-token-value",
        dispatch_continuations=_noop_dispatch,
    )
    return tenant_id, token


async def test_mcp_missing_server_url_records_write_failed_rather_than_staying_pending(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A row that lost the server it named cannot be saved against anything."""
    tenant_id, token = await _run_mcp_submission(
        db_session, db_session_factory, mcp_server_url=None, posted_message_id=None
    )

    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
    assert request_row is not None and request_row.outcome == "write_failed", (
        "the spent request must not sit with no recorded outcome"
    )
    assert await _pending_continuations(db_session_factory, tenant_id=tenant_id) == [], (
        "nothing was saved, so no turn is owed"
    )


async def test_success_paths_send_no_ephemeral_receipt(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The card is the receipt (both would say the same thing twice)."""
    tenant_id, token = await _run_mcp_submission(db_session, db_session_factory, attach_fails=False)

    assert ("POST", _EPHEMERAL_URL) not in fake_slack_web_client.mock.requests, (
        "a successful submission says it once, on the card"
    )
    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
    assert request_row is not None and request_row.outcome == "applied"
    assert _chat_updates(fake_slack_web_client)[-1]["text"] == (
        "✅ tester is connected to my-server."
    ), "the applied card carries the shared change-confirmation copy"
    pending = await _pending_continuations(db_session_factory, tenant_id=tenant_id)
    assert [row.requested_work for row in pending] == [_WORK], (
        "a connected server resumes the work that was waiting on it"
    )


@pytest.mark.parametrize("failure", ["rejection", "timeout"])
async def test_mcp_submission_refuses_a_token_the_server_rejects_before_any_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Same door check as Discord: a 401/403 from the server stores nothing."""
    import dataclasses

    from daimon.core import credential_submit
    from daimon.core.mcp_oauth import McpProbe

    if failure == "timeout":
        monkeypatch.setattr(credential_submit, "CREDENTIAL_SAVE_TIMEOUT_SECONDS", 0.1)
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    account_id = derive_guild_account_uuid(tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="mcp",
        target="notion",
        mcp_server_url="https://mcp.notion.com/mcp",
        agent_id=agent_id,
        posted_message_id=_MESSAGE_TS,
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()
    ma_calls: list[str] = []
    counting = _mcp_handler(live_agent, account_id=account_id, agent_id=agent_id)

    def handler(req: httpx.Request) -> httpx.Response:
        ma_calls.append(f"{req.method} {req.url.path}")
        return counting(req)

    accepted = False

    async def probe(url: str, value: str) -> McpProbe:
        if failure == "timeout" and not accepted:
            await asyncio.Event().wait()
        return McpProbe(status_code=200 if accepted else 401, resource_metadata_url=None)

    runtime = dataclasses.replace(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=handler, mcp_configured=True
        ),
        mcp_token_probe=probe,
    )
    runtime.turn_deps.fernet = build_multifernet((fernet_key,))
    await run_mcp_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="ntn_rejected",
        dispatch_continuations=_noop_dispatch,
    )

    assert not any(c.startswith("POST") for c in ma_calls), "nothing is written to MA"
    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
    assert request_row is not None and request_row.outcome == (
        "token_rejected" if failure == "rejection" else "write_failed"
    )
    card = _chat_updates(fake_slack_web_client)[-1]
    assert ("That token was rejected" if failure == "rejection" else "too long") in card["text"]
    assert "Try again" in card["text"]
    if failure == "rejection":
        assert any(
            "connect it with your account" in t for t in _ephemeral_texts(fake_slack_web_client)
        ), "the person is pointed at the OAuth path"

    assert request_row.used_at is None
    assert RECEIVED_FOOTER not in json.dumps(card)
    assert any(block["type"] == "actions" for block in card["blocks"])
    accepted = True
    monkeypatch.setattr(credential_submit, "CREDENTIAL_SAVE_TIMEOUT_SECONDS", 90.0)
    await run_mcp_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="ntn_corrected",
        dispatch_continuations=_noop_dispatch,
    )
    async with db_session_factory() as session:
        saved = await peek_credential_request(session, token=token)
    assert saved is not None and saved.outcome == "applied" and saved.used_at is not None
    assert "✅" in _chat_updates(fake_slack_web_client)[-1]["text"]


# ---------------------------------------------------------------------------
# No crypto keys: every key form refuses with the operator step
# ---------------------------------------------------------------------------


def _keyless(factory: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
    """The same database, as a deployment with no DAIMON_CRYPTO__KEYS sees it."""
    return async_sessionmaker(
        bind=factory.kw["bind"],
        expire_on_commit=False,
        info={"crypto_keys": (), "crypto_allow_plaintext": False},
    )


@pytest.mark.asyncio
async def test_env_submission_without_crypto_keys_tells_the_person_and_keeps_the_request(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        tenant_id=tenant_id,
        kind="env",
    )
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key, _keyless(db_session_factory), anthropic_handler=_agents_handler(live_agent)
    )

    await run_env_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="s3cr3t-value",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert rows == [], "nothing is stored without encryption keys"
    assert row is not None and row.used_at is None, "the request stays live for a retry"
    texts = _ephemeral_texts(fake_slack_web_client)
    assert texts and "DAIMON_CRYPTO__KEYS" in texts[-1], "the person learns the operator step"


@pytest.mark.asyncio
async def test_env_file_submission_without_crypto_keys_tells_the_person_and_keeps_the_request(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await db_session.commit()
    _patch_file_download(monkeypatch, b"ALPHA_KEY=alpha-private\n")
    runtime = _build_runtime(
        fernet_key, _keyless(db_session_factory), anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert rows == [], "nothing is stored without encryption keys"
    assert row is not None and row.used_at is None, "the request stays live for a retry"
    texts = _ephemeral_texts(fake_slack_web_client)
    assert texts and "DAIMON_CRYPTO__KEYS" in texts[-1], "the person learns the operator step"


@pytest.mark.parametrize(
    "target", ["R_PROFILE_USER", "TAR_OPTIONS", "SNOWFLAKE_USER", "AWS_REGION"]
)
async def test_env_submission_member_cannot_store_a_non_secret_name(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    target: str,
) -> None:
    """The submitter's live Slack role decides the name: a member may store secrets only."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        target=target,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
        value="private-value",
    )

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        request_row = await peek_credential_request(s, token=token)
    assert rows == [], f"a member must not store {target}"
    assert request_row is not None and request_row.used_at is None, "a refusal spends nothing"
    ephemeral = fake_slack_web_client.mock.requests[("POST", _EPHEMERAL_URL)][-1].kwargs["json"]
    assert target in ephemeral["text"], "the refusal names the key"
    assert "private-value" not in json.dumps(ephemeral), "the value never reaches a message"


async def test_env_submission_admin_may_store_an_identity_name(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        target="SNOWFLAKE_USER",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()
    _override_users_info_admin(fake_slack_web_client.mock)

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
    )

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
    assert [r["key"] for r in rows] == ["SNOWFLAKE_USER"], "an admin may add an identity name"


async def test_mcp_attach_failure_publishes_no_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The attach comes before the token is published: when it fails, nothing is
    stored, no other session can mirror anything, and no work resumes."""
    tenant_id, token = await _run_mcp_submission(db_session, db_session_factory, attach_fails=True)

    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
        stored = (
            await session.execute(text("SELECT count(*) FROM agent_mcp_credentials"))
        ).scalar_one()
    assert request_row is not None and request_row.outcome == "write_failed"
    assert stored == 0, "no agent-wide token is published without an attach"
    card = _chat_updates(fake_slack_web_client)[-1]
    assert "Try again" in card["text"]
    assert request_row.used_at is None
    assert await _pending_continuations(db_session_factory, tenant_id=tenant_id) == []


async def test_mcp_vault_write_failure_after_the_attach_renders_partial(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The submitter's own vault copy is written last; when it fails the server is
    attached and the shared token published, so the card is partial and no work
    resumes."""
    tenant_id, token = await _run_mcp_submission(db_session, db_session_factory, vault_fails=True)

    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
    assert request_row is not None and request_row.outcome == "write_failed"
    card = _chat_updates(fake_slack_web_client)[-1]
    assert "The connection did not finish" in json.dumps(card)
    assert RECEIVED_FOOTER not in json.dumps(card), "the card must leave the received state"
    pending = await _pending_continuations(db_session_factory, tenant_id=tenant_id)
    assert pending == [], "failed attempts cannot occupy the retry continuation"


async def test_mcp_agent_gone_before_the_attach_saves_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The agent did not survive the form: the attach comes first, so nothing is
    stored anywhere, and the card says so."""
    tenant_id, token = await _run_mcp_submission(
        db_session, db_session_factory, agent_gone_after_write=True
    )

    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
        stored = (
            await session.execute(text("SELECT count(*) FROM agent_mcp_credentials"))
        ).scalar_one()
    assert request_row is not None and request_row.outcome == "write_failed"
    assert stored == 0
    card = _chat_updates(fake_slack_web_client)[-1]
    assert "Try again" in card["text"]
    assert request_row.used_at is None
    assert await _pending_continuations(db_session_factory, tenant_id=tenant_id) == []


@pytest.mark.parametrize("admin", [False, True], ids=["member-refused", "admin-allowed"])
async def test_mcp_submission_repointing_an_existing_server_on_a_shared_agent_needs_an_admin(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    admin: bool,
) -> None:
    """H2: a member must not repoint a live agent's server at another URL.

    The agent already declares `my-server` elsewhere and is the deployment
    default, so a member's submission is refused before any vault write or
    attach; an admin's goes through.
    """
    import dataclasses

    from daimon.core.scope import DeploymentDefault

    if admin:
        _override_users_info_admin(fake_slack_web_client.mock)
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(
        id="agent_credentials",
        name="specialist",
        tenant_id=tenant_id,
        mcp_servers=[{"name": "my-server", "type": "url", "url": "https://real.example.com/mcp"}],
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    account_id = derive_guild_account_uuid(tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="mcp",
        target="my-server",
        mcp_server_url=_MCP_SERVER_URL,
        agent_id=agent_id,
        posted_message_id=_MESSAGE_TS,
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()
    ma_calls: list[str] = []
    inner = _mcp_handler(live_agent, account_id=account_id, agent_id=agent_id)

    def handler(req: httpx.Request) -> httpx.Response:
        ma_calls.append(f"{req.method} {req.url.path}")
        return inner(req)

    runtime = dataclasses.replace(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=handler, mcp_configured=True
        ),
        deployment_default=DeploymentDefault(agent_name="specialist"),
    )
    runtime.turn_deps.fernet = build_multifernet((fernet_key,))
    await run_mcp_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="attacker-token",
        dispatch_continuations=_noop_dispatch,
    )

    writes = [c for c in ma_calls if c.startswith("POST")]
    async with db_session_factory() as session:
        request_row = await peek_credential_request(session, token=token)
        stored = (
            await session.execute(text("SELECT count(*) FROM agent_mcp_credentials"))
        ).scalar_one()
    assert request_row is not None
    if admin:
        assert request_row.outcome == "applied"
        assert f"POST /v1/agents/{live_agent.id}" in writes
    else:
        assert writes == [], "a refused replacement writes no vault token and no attach"
        assert stored == 0, "the agent-wide token must not be overwritten"
        assert request_row.outcome == "write_failed"
        assert any("admin" in t for t in _ephemeral_texts(fake_slack_web_client))


async def test_mcp_submission_member_cannot_overwrite_the_shared_token_of_a_connected_server(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """H2: the agent already uses the URL and has its agent-wide token; a member's
    paste must not replace it."""
    import dataclasses

    from daimon.core.agent_mcp_credentials import (
        resolve_agent_mcp_credentials,
        save_agent_mcp_credential,
    )
    from daimon.core.scope import DeploymentDefault

    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(
        id="agent_credentials",
        name="specialist",
        tenant_id=tenant_id,
        mcp_servers=[{"name": "my-server", "type": "url", "url": _MCP_SERVER_URL}],
    )
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    account_id = derive_guild_account_uuid(tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="mcp",
        target="my-server",
        mcp_server_url=_MCP_SERVER_URL,
        agent_id=agent_id,
        posted_message_id=_MESSAGE_TS,
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()
    fernet = build_multifernet((fernet_key,))
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant_id,
        agent_id=agent_id,
        mcp_server_url=_MCP_SERVER_URL,
        plaintext_token="the-real-token",
    )
    ma_calls: list[str] = []
    inner = _mcp_handler(live_agent, account_id=account_id, agent_id=agent_id)

    def handler(req: httpx.Request) -> httpx.Response:
        ma_calls.append(f"{req.method} {req.url.path}")
        return inner(req)

    runtime = dataclasses.replace(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=handler, mcp_configured=True
        ),
        deployment_default=DeploymentDefault(agent_name="specialist"),
    )
    runtime.turn_deps.fernet = fernet
    await run_mcp_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="attacker-token",
        dispatch_continuations=_noop_dispatch,
    )

    stored = await resolve_agent_mcp_credentials(
        sessionmaker=db_session_factory, fernet=fernet, tenant_id=tenant_id, agent_id=agent_id
    )
    assert [c.token for c in stored] == ["the-real-token"]
    assert not [c for c in ma_calls if c.startswith("POST")]
    assert any("admin" in t for t in _ephemeral_texts(fake_slack_web_client))


async def test_mcp_submission_admin_token_written_after_the_members_decision_is_not_overwritten(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Barrier: member admitted (no token yet) -> admin saves the first shared token ->
    member resumes. The store re-checks under its lock: admin's ciphertext stays."""
    from daimon.core.agent_mcp_credentials import save_agent_mcp_credential

    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    account_id = derive_guild_account_uuid(tenant_id=tenant_id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="mcp",
        target="my-server",
        mcp_server_url=_MCP_SERVER_URL,
        agent_id=agent_id,
        posted_message_id=_MESSAGE_TS,
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()
    fernet = build_multifernet((fernet_key,))
    ma_calls: list[str] = []
    inner = _mcp_handler(live_agent, account_id=account_id, agent_id=agent_id)

    def handler(req: httpx.Request) -> httpx.Response:
        ma_calls.append(f"{req.method} {req.url.path}")
        return inner(req)

    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=handler, mcp_configured=True
    )
    runtime.turn_deps.fernet = fernet
    real_decide = credential_submissions_mod.decide_mcp_connect

    async def decide_then_admin_writes(*args: Any, **kwargs: Any) -> Any:
        decision = await real_decide(*args, **kwargs)
        assert not decision.replaces
        await save_agent_mcp_credential(
            sessionmaker=db_session_factory,
            fernet=fernet,
            tenant_id=tenant_id,
            agent_id=agent_id,
            mcp_server_url=_MCP_SERVER_URL,
            plaintext_token="admin-token",
        )
        return decision

    monkeypatch.setattr(credential_submissions_mod, "decide_mcp_connect", decide_then_admin_writes)
    await run_mcp_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="member-token",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as session:
        stored = (
            await session.execute(text("SELECT encrypted_token FROM agent_mcp_credentials"))
        ).scalar_one()
    assert fernet.decrypt(stored).decode() == "admin-token"
    assert not [c for c in ma_calls if c.startswith("POST /v1/vaults")], "no vault write"
    assert any("admin" in t for t in _ephemeral_texts(fake_slack_web_client))


async def test_env_submission_alias_of_a_held_key_needs_the_replacement_gate(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """ANTHROPIC_AUTH_TOKEN beside a held ANTHROPIC_API_KEY is refused for a member on a live agent."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(
        id="agent_credentials",
        name="specialist",
        tenant_id=tenant_id,
        metadata={MA_METADATA_KEY_MANAGED: "true"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_uuid,
        key="ANTHROPIC_API_KEY",
        content="the-value-in-use",
        set_by_account_id=None,
    )
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        target="ANTHROPIC_AUTH_TOKEN",
        agent_id=agent_uuid,
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
    )

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        request_row = await peek_credential_request(s, token=token)
    assert [r["key"] for r in rows] == ["ANTHROPIC_API_KEY"], "the alias must not be stored"
    assert request_row is not None and request_row.outcome == "write_failed"
    edit = _chat_updates(fake_slack_web_client)[-1]
    assert "Adding ANTHROPIC_AUTH_TOKEN would replace ANTHROPIC_API_KEY" in json.dumps(edit), (
        "the card names both keys"
    )


@pytest.mark.asyncio
async def test_env_file_submission_refuses_an_alias_of_a_held_key_and_names_both(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        key="GH_TOKEN",
        content="the-value-in-use",
        set_by_account_id=None,
    )
    await db_session.commit()
    _patch_file_download(monkeypatch, b"OPENAI_API_KEY=a\nGITHUB_TOKEN=secret-b\n")
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
    assert [r["key"] for r in rows] == ["GH_TOKEN"], (
        "an import that shadows a held key writes nothing"
    )
    edit = json.dumps(_chat_updates(fake_slack_web_client)[-1])
    assert "GITHUB_TOKEN would replace GH_TOKEN" in edit, "the refusal names both keys"
    assert "secret-b" not in edit, "no value on the card"


async def test_env_submission_alias_that_appears_after_the_gate_is_caught_under_the_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias read before the gate is repeated inside the write transaction."""

    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        target="GITHUB_TOKEN",
        agent_id=agent_uuid,
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
    )
    await db_session.commit()

    real = credential_submit.list_turn_key_names
    calls = 0

    async def first_read_misses_the_alias(session: AsyncSession, **kw: Any) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ()
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            key="GH_TOKEN",
            content="the-value-in-use",
            set_by_account_id=None,
        )
        return await real(session, **kw)

    monkeypatch.setattr(credential_submit, "list_turn_key_names", first_read_misses_the_alias)

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
    )

    assert calls == 2, "the alias must be re-read under the write"
    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        request_row = await peek_credential_request(s, token=token)
    assert [r["key"] for r in rows] == ["GH_TOKEN"], "the late alias blocks the write"
    assert request_row is not None and request_row.outcome == "stale_replacement"


async def _aws_pair_and_secret_token(
    db_session: AsyncSession, tenant_id: uuid.UUID, live_agent: Any
) -> str:
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        key="AWS_ACCESS_KEY_ID",
        content="AKIAEXAMPLE",
        set_by_account_id=None,
    )
    secret = await put_agent_file(
        db_session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        key="AWS_SECRET_ACCESS_KEY",
        content="old-secret",
        set_by_account_id=None,
    )
    token = await _seed_request(
        db_session,
        tenant_id=tenant_id,
        kind="env",
        target="AWS_SECRET_ACCESS_KEY",
        agent_id=agent_id,
        origin_thread_id=_ORIGIN_THREAD,
        target_ma_agent_id=live_agent.id,
        replaces_updated_at=secret.updated_at,
    )
    await db_session.commit()
    return token


async def _stored_secret(db_session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
    return next(r["content"] for r in rows if r["key"] == "AWS_SECRET_ACCESS_KEY")


async def test_an_admin_can_rotate_one_key_of_a_stored_aws_family(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The stored AWS_ACCESS_KEY_ID is a family member, not a newly appeared conflict."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(
        id="agent_credentials",
        name="specialist",
        tenant_id=tenant_id,
        metadata={MA_METADATA_KEY_MANAGED: "true"},
    )
    token = await _aws_pair_and_secret_token(db_session, tenant_id, live_agent)
    _override_users_info_admin(fake_slack_web_client.mock)

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
        value="new-secret",
    )

    assert await _stored_secret(db_session_factory) == "new-secret", "the rotation lands"
    async with db_session_factory() as s:
        request_row = await peek_credential_request(s, token=token)
    assert request_row is not None and request_row.outcome == "applied"


async def test_a_family_change_during_an_admin_rotation_is_still_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(
        id="agent_credentials",
        name="specialist",
        tenant_id=tenant_id,
        metadata={MA_METADATA_KEY_MANAGED: "true"},
    )
    token = await _aws_pair_and_secret_token(db_session, tenant_id, live_agent)
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id)
    _override_users_info_admin(fake_slack_web_client.mock)
    real = credential_submit.list_turn_key_names
    calls = 0

    async def family_changes_after_the_gate(session: AsyncSession, **kw: Any) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            await put_agent_file(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                key="AWS_SESSION_TOKEN",
                content="grafted",
                set_by_account_id=None,
            )
        return await real(session, **kw)

    monkeypatch.setattr(credential_submit, "list_turn_key_names", family_changes_after_the_gate)

    await _run_env(
        _build_runtime(
            fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
        ),
        token,
        value="new-secret",
    )

    assert await _stored_secret(db_session_factory) == "old-secret"
    async with db_session_factory() as s:
        request_row = await peek_credential_request(s, token=token)
    assert request_row is not None and request_row.outcome == "stale_replacement"


@pytest.mark.asyncio
async def test_env_file_submission_that_fails_to_save_replies_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """An unexpected save error is answered, not left to escape into the spawned task."""
    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_env_file", name="specialist", tenant_id=tenant_id)
    token = await _seed_env_file_request(db_session, tenant_id=tenant_id, live_agent=live_agent)
    await db_session.commit()
    _patch_file_download(monkeypatch, b"ALPHA_TOKEN=alpha-private\n")

    async def _broken_apply(*args: object, **kwargs: object) -> Any:
        raise RuntimeError("database went away")

    monkeypatch.setattr(credential_submissions_mod, "_apply_env_file_entries", _broken_apply)
    runtime = _build_runtime(
        fernet_key, db_session_factory, anthropic_handler=_agents_handler(live_agent)
    )

    await _run_env_file_submission(runtime, token)

    texts = _ephemeral_texts(fake_slack_web_client)
    assert texts and "Something went wrong" in texts[-1]
    async with db_session_factory() as s:
        row = await peek_credential_request(s, token=token)
    assert row is not None and row.used_at is None, "the request stays live for a retry"


@pytest.mark.asyncio
async def test_env_submission_decides_a_late_pin_inside_the_consume(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pin landing after the early check is decided in the consume transaction."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.agent_pins import PIN_WRITE_REFUSAL
    from daimon.core.stores.access_policy import set_access_policy

    tenant_id, fernet_key = await _seed_team(db_session)
    live_agent = ma_agent(id="agent_credentials", name="specialist", tenant_id=tenant_id)

    def ma_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return list_response([live_agent.model_dump(mode="json")])
        raise AssertionError(f"Unexpected MA request: {request.method} {request.url.path}")

    token = await _seed_request(
        db_session,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=live_agent.id),
        tenant_id=tenant_id,
        kind="env",
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant_id,
        policy=TenantAccessPolicy(agent_channel_pins={"specialist": ("C_ELSEWHERE",)}),
    )
    await db_session.commit()

    async def early_check_passes(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(credential_submissions_mod, "request_pin_refusal", early_check_passes)
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=ma_handler)

    await run_env_credential_submission(
        runtime,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        token=token,
        value="s3cr3t-value",
        dispatch_continuations=_noop_dispatch,
    )

    async with db_session_factory() as s:
        rows = await _agent_file_rows(s)
        row = await peek_credential_request(s, token=token)
    assert rows == [], "nothing is saved"
    assert row is not None and row.used_at is None, "the refused form was not spent"
    assert PIN_WRITE_REFUSAL in _ephemeral_texts(fake_slack_web_client)

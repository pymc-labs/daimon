"""Credential-request dialogs, driven through the real SDK route: open, submit, card edits.

Only the Bot Framework transport, MA and the MCP vault writes are faked.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from cryptography.fernet import Fernet
from daimon.adapters.teams import credential_requests as module
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.mcp_oauth.discovery import McpProbe
from daimon.core.posted_controls import (
    EXPIRED_HEADLINE,
    NO_LONGER_VALID_MESSAGE,
    WRONG_REQUESTER_MESSAGE,
)
from daimon.core.posted_controls.teams_card import CREDENTIAL_DIALOG
from daimon.core.stores.agent_files import get_agent_file, put_agent_file
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.tenants import get_tenant
from daimon.testing import build_fake_anthropic, ma_agent
from daimon.testing.factories import make_account
from daimon.testing.ma import MARouter
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    TeamsApiFake,
    build_teams_runtime,
    make_invoke,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
MA_ID = "agt_1"
SECRET = "s3cr3t-never-shown"
MCP_URL = "https://mcp.example.com/mcp"


@pytest.fixture
async def account_id(
    db_session_factory: async_sessionmaker[AsyncSession], provisioned_tenant: None
) -> uuid.UUID:
    async with db_session_factory.begin() as session:
        tenant = await get_tenant(session, TENANT)
        assert tenant is not None
        return (await make_account(session, tenant=tenant)).id


def _runtime(
    db: async_sessionmaker[AsyncSession], *, managed: bool = False, mcp: bool = False
) -> TeamsRuntime:
    router = MARouter()
    metadata = {MA_METADATA_KEY_MANAGED: "true"} if managed else None
    router.add_agent_list(ma_agent(id=MA_ID, name="daimon", tenant_id=TENANT, metadata=metadata))
    runtime = build_teams_runtime(db, anthropic=build_fake_anthropic(router.dispatch))
    if mcp:
        runtime.settings.mcp.public_url = "https://daimon.example.com/mcp"
        runtime.settings.mcp.jwt_secret = SecretStr("j" * 32)
        runtime.settings.mcp.app_root_url = "https://daimon.example.com"
        fernet = build_multifernet((Fernet.generate_key().decode(),))
        runtime = dataclasses.replace(
            runtime, turn_deps=dataclasses.replace(runtime.turn_deps, fernet=fernet)
        )
    return runtime


async def _request(
    db: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    *,
    kind: str = "env",
    expires_in: timedelta = timedelta(minutes=10),
    replaces: datetime | None = None,
    target: str | None = None,
) -> CredentialRequestRow:
    async with db.begin() as session:
        return await create_credential_request(
            session,
            token=uuid.uuid4().hex,
            kind=kind,  # pyright: ignore[reportArgumentType]
            tenant_id=TENANT,
            agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id=MA_ID),
            account_id=account_id,
            target=target or ("API_KEY" if kind == "env" else "linear"),
            mcp_server_url=None if kind == "env" else MCP_URL,
            requester_platform_user_id=AAD_OBJECT_ID,
            channel_id=CONVERSATION_ID,
            expires_at=datetime.now(UTC) + expires_in,
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=MA_ID,
            target_name="daimon",
            requested_work="finish the report with the key",
            replaces_updated_at=replaces,
            platform="teams",
            origin_thread_id=CONVERSATION_ID,
            posted_message_id="m-7",
        )


@asynccontextmanager
async def _running(
    fake: TeamsApiFake, runtime: TeamsRuntime
) -> AsyncIterator[tuple[TeamsHttpService, AsyncMock]]:
    async with running_service(runtime, fake) as service:
        dispatch = AsyncMock()
        service.turns.credentials._dispatch = dispatch  # pyright: ignore[reportPrivateUsage]
        yield service, dispatch


def _invoke(name: str, data: dict[str, object], **kw: Any) -> dict[str, object]:
    return make_invoke(name, {"data": data}, **kw)


def _open(token: str, **kw: Any) -> dict[str, object]:
    data = {"msteams": {"type": "task/fetch"}, "dialog_id": CREDENTIAL_DIALOG, "token": token}
    return _invoke("task/fetch", data, **kw)


def _submit(token: str, secret: str = SECRET, **kw: Any) -> dict[str, object]:
    return _invoke("task/submit", {"action": module.SUBMIT, "token": token, "secret": secret}, **kw)


def _edits(fake: TeamsApiFake) -> list[str]:
    return [
        json.dumps(r.body, ensure_ascii=False)
        for r in fake.requests
        if r.method == "PUT" and r.url.endswith("/activities/m-7")
    ]


async def test_only_the_requester_gets_the_password_form(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id)
    async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, _):
        form = await post_activity(service, _open(row.token))
        stranger = await post_activity(service, _open(row.token, user=OTHER_AAD_OBJECT_ID))
        elsewhere = await post_activity(service, _open(row.token, chat="a:other-chat"))
        unknown = await post_activity(service, _open("nope"))

    card = form["task"]["value"]["card"]["content"]
    field = next(item for item in card["body"] if item.get("id") == "secret")
    assert field["style"] == "Password" and "value" not in field, "masked, never prefilled"
    assert card["actions"][0]["data"] == {"action": module.SUBMIT, "token": row.token}
    assert stranger["task"]["value"] == WRONG_REQUESTER_MESSAGE
    assert elsewhere["task"]["value"] == unknown["task"]["value"] == NO_LONGER_VALID_MESSAGE


async def test_a_late_click_marks_the_card_expired_only_for_the_requester(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id, expires_in=timedelta(seconds=-1))
    fake = TeamsApiFake()
    async with _running(fake, _runtime(db_session_factory)) as (service, _):
        stranger = await post_activity(service, _open(row.token, user=OTHER_AAD_OBJECT_ID))
        await service.turns.drain(5)
        untouched = _edits(fake)
        late = await post_activity(service, _open(row.token))
        await service.turns.drain(5)

    assert stranger["task"]["value"] == WRONG_REQUESTER_MESSAGE and not untouched
    assert late["task"]["value"].startswith(EXPIRED_HEADLINE)
    assert EXPIRED_HEADLINE in _edits(fake)[0]


async def test_an_env_value_is_saved_once_and_resumes_the_work(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id)
    fake = TeamsApiFake()
    with structlog.testing.capture_logs() as logs:
        async with _running(fake, _runtime(db_session_factory)) as (service, dispatch):
            empty = await post_activity(service, _submit(row.token, secret="  "))
            big = await post_activity(service, _submit(row.token, secret="x" * 5000))
            saved = await post_activity(service, _submit(row.token))
            await service.turns.drain(5)
            again = await post_activity(service, _submit(row.token, secret="other"))

    assert "cannot be empty" in json.dumps(empty) and "too large" in json.dumps(big)
    assert "x" * 5000 not in json.dumps(big), "a rejected value is not echoed back"
    assert not (saved or {}).get("task"), "the dialog closes"
    assert again["task"]["value"] == NO_LONGER_VALID_MESSAGE, "single use"
    async with db_session_factory() as session:
        file = await get_agent_file(session, tenant_id=TENANT, agent_id=row.agent_id, key="API_KEY")
        spent = await peek_credential_request(session, token=row.token)
        queued = await get_continuation(session, idempotency_key=row.idempotency_key)
    assert file is not None and file.content == SECRET
    assert spent is not None and spent.outcome == "applied"
    assert queued is not None and queued.requested_work == "finish the report with the key"
    assert "✅" in _edits(fake)[-1], "the card becomes the receipt"
    dispatch.assert_awaited_once_with(TENANT, CONVERSATION_ID, SERVICE_URL)
    assert SECRET not in json.dumps([r.body for r in fake.requests]) + repr(logs)


@pytest.mark.parametrize(
    ("target", "admin_ok"), [("1BAD", False), ("LD_PRELOAD", False), ("SERVICE_URL", True)]
)
async def test_a_key_name_the_submitter_may_not_store_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    target: str,
    admin_ok: bool,
) -> None:
    row = await _request(db_session_factory, account_id, target=target)
    problem = module.env_name_problem(target)
    expected = module.env_name_refusal(target, problem or "not_credential_name")
    assert (module.env_name_problem(target, is_admin=True) is None) == admin_ok
    async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, dispatch):
        refused = await post_activity(service, _submit(row.token))
        await service.turns.drain(5)
    assert refused["task"]["value"] == expected
    async with db_session_factory() as session:
        file = await get_agent_file(session, tenant_id=TENANT, agent_id=row.agent_id, key=target)
        live = await peek_credential_request(session, token=row.token)
    assert file is None and live is not None and live.used_at is None, "nothing spent or saved"
    dispatch.assert_not_awaited()


async def test_an_env_value_is_refused_without_encryption_keys(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id)
    with patch.object(module, "agent_env_writes_allowed", return_value=False):
        async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, _):
            refused = await post_activity(service, _submit(row.token))
            await service.turns.drain(5)
    assert "DAIMON_CRYPTO__KEYS" in refused["task"]["value"]
    async with db_session_factory() as session:
        live = await peek_credential_request(session, token=row.token)
    assert live is not None and live.used_at is None, "the request stays live for a retry"


async def test_a_failed_submit_reports_nothing_that_could_carry_the_value(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id)
    boom = AsyncMock(side_effect=DaimonError("boom"))
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(module, "find_agent_by_derived_uuid", boom),
        patch("daimon.adapters.teams.card_actions.capture_exception_with_scope") as sentry,
    ):
        async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, _):
            failed = await post_activity(service, _submit(row.token))
    assert failed["task"]["value"] == module.FAILED
    sentry.assert_not_called()
    assert logs[-1] == {
        "event": "teams.credential.failed",
        "err_type": "DaimonError",
        "log_level": "error",
    }


async def test_a_member_cannot_replace_a_key_on_a_managed_agent(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    agent_id = derive_agent_uuid(tenant_id=TENANT, ma_agent_id=MA_ID)
    async with db_session_factory.begin() as session:
        old = await put_agent_file(
            session,
            tenant_id=TENANT,
            agent_id=agent_id,
            key="API_KEY",
            content="old",
            set_by_account_id=account_id,
        )
    row = await _request(db_session_factory, account_id, replaces=old.updated_at)
    fake = TeamsApiFake()
    runtime = _runtime(db_session_factory, managed=True)
    async with _running(fake, runtime) as (service, dispatch):
        await post_activity(service, _submit(row.token))
        await service.turns.drain(5)

    async with db_session_factory() as session:
        file = await get_agent_file(session, tenant_id=TENANT, agent_id=agent_id, key="API_KEY")
    assert file is not None and file.content == "old", "the existing key is unchanged"
    assert "was not replaced" in _edits(fake)[-1]
    dispatch.assert_not_awaited()


async def test_an_mcp_token_needs_setup_and_a_token_the_server_accepts(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id, kind="mcp")
    async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, _):
        unconfigured = await post_activity(service, _submit(row.token))
    assert "not finished being set up" in unconfigured["task"]["value"]

    probe = AsyncMock(return_value=McpProbe(status_code=401, resource_metadata_url=None))
    runtime = dataclasses.replace(_runtime(db_session_factory, mcp=True), mcp_token_probe=probe)
    fake, store = TeamsApiFake(), AsyncMock()
    with patch.object(module, "add_external_mcp_credential", store):
        async with _running(fake, runtime) as (service, dispatch):
            await post_activity(service, _submit(row.token))
            await service.turns.drain(5)

    store.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert "did not accept that token" in _edits(fake)[-1]


async def test_an_mcp_token_is_stored_attached_and_resumes_the_work(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id, kind="mcp")
    fake, store, attach = TeamsApiFake(), AsyncMock(), AsyncMock()
    with (
        patch.object(module, "add_external_mcp_credential", store),
        patch.object(module, "attach_mcp_server_to_agent", attach),
    ):
        async with _running(fake, _runtime(db_session_factory, mcp=True)) as (service, dispatch):
            await post_activity(service, _submit(row.token))
            await service.turns.drain(5)

    assert store.await_args is not None and store.await_args.kwargs["token"] == SECRET
    assert attach.await_args is not None and attach.await_args.kwargs["url"] == MCP_URL
    assert "✅" in _edits(fake)[-1]
    dispatch.assert_awaited_once_with(TENANT, CONVERSATION_ID, SERVICE_URL)


async def test_an_oauth_click_hands_the_requester_a_private_sign_in_link(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id, kind="mcp_oauth")
    fake = TeamsApiFake()
    async with _running(fake, _runtime(db_session_factory, mcp=True)) as (service, _):
        link = await post_activity(service, _open(row.token))
        again = await post_activity(service, _open(row.token))
        await service.turns.drain(5)

    (button,) = link["task"]["value"]["card"]["content"]["actions"]
    assert button["type"] == "Action.OpenUrl"
    assert button["url"].startswith("https://daimon.example.com/")
    assert again["task"]["value"] != link["task"]["value"], "the link is handed out once"
    assert _edits(fake), "the card stops offering the button"

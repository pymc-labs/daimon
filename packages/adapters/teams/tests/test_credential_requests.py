"""Credential-request dialogs, driven through the real SDK route: open, submit, card edits.

Only the Bot Framework transport, MA and the MCP vault writes are faked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from cryptography.fernet import Fernet
from daimon.adapters.teams import credential_requests as module
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core import credential_submit
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import PIN_WRITE_REFUSAL
from daimon.core.credential_requests import ENV_FILE_TARGET, build_skill_repo_target
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.report import Action, ResourceOutcome
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
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.agent_files import get_agent_file, list_agent_files, put_agent_file
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.agent_skill_repo_credentials import get_skill_repo_credential
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.tenants import get_tenant
from daimon.testing import build_fake_anthropic, ma_agent
from daimon.testing.factories import make_account
from daimon.testing.ma import MARouter, list_response
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
_TARGETS = {
    "env": "API_KEY",
    "env_file": ENV_FILE_TARGET,
    "repo": build_skill_repo_target("https://github.com/o/r", "release", ""),
    "skill_repo": build_skill_repo_target("https://github.com/o/skills", "main", "skills"),
}


@pytest.fixture
async def account_id(
    db_session_factory: async_sessionmaker[AsyncSession], provisioned_tenant: None
) -> uuid.UUID:
    async with db_session_factory.begin() as session:
        tenant = await get_tenant(session, TENANT)
        assert tenant is not None
        return (await make_account(session, tenant=tenant)).id


def _runtime(
    db: async_sessionmaker[AsyncSession],
    *,
    managed: bool = False,
    mcp: bool = False,
    updates: list[dict[str, Any]] | None = None,
) -> TeamsRuntime:
    """`updates` collects each agent update, for the skill attach."""
    router = MARouter()
    metadata = {MA_METADATA_KEY_MANAGED: "true"} if managed else None
    agent = ma_agent(id=MA_ID, name="daimon", tenant_id=TENANT, metadata=metadata)
    router.add_agent_list(agent)
    router.add_agent(agent)
    router.add("GET", r"/v1/skills", lambda _r, _m: list_response([]))

    def update(request: httpx.Request, _match: object) -> httpx.Response:
        (updates if updates is not None else []).append(json.loads(request.content))
        return httpx.Response(200, json=agent.model_dump(mode="json"))

    router.add("POST", rf"/v1/agents/{MA_ID}", update)
    runtime = build_teams_runtime(db, anthropic=build_fake_anthropic(router.dispatch))
    if mcp:
        runtime.settings.mcp.public_url = "https://daimon.example.com/mcp"
        runtime.settings.mcp.jwt_secret = SecretStr("j" * 32)
        runtime.settings.mcp.app_root_url = "https://daimon.example.com"
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    return dataclasses.replace(
        runtime, turn_deps=dataclasses.replace(runtime.turn_deps, fernet=fernet)
    )


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
            target=target or _TARGETS.get(kind, "linear"),
            mcp_server_url=MCP_URL if kind in ("mcp", "mcp_oauth") else None,
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


def _field(form: dict[str, Any]) -> dict[str, Any]:
    card = form["task"]["value"]["card"]["content"]
    return next(item for item in card["body"] if item.get("id") == "secret")


async def test_only_the_requester_gets_the_private_form(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id)
    async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, _):
        form = await post_activity(service, _open(row.token))
        stranger = await post_activity(service, _open(row.token, user=OTHER_AAD_OBJECT_ID))
        elsewhere = await post_activity(service, _open(row.token, chat="a:other-chat"))
        unknown = await post_activity(service, _open("nope"))

    card = form["task"]["value"]["card"]["content"]
    assert "value" not in _field(form), "never prefilled"
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


@pytest.mark.parametrize("failure", ["rejection", "slow_rejection"])
async def test_an_mcp_token_needs_setup_and_a_token_the_server_accepts(
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "slow_rejection":
        monkeypatch.setattr(credential_submit, "CREDENTIAL_CARD_DEADLINE_SECONDS", 0.1)
    row = await _request(db_session_factory, account_id, kind="mcp")
    async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, _):
        unconfigured = await post_activity(service, _submit(row.token))
    assert "not finished being set up" in unconfigured["task"]["value"]

    accepted = False

    async def probe_impl(url: str, token: str) -> McpProbe:
        if failure == "slow_rejection" and not accepted:
            await asyncio.sleep(0.2)
        return McpProbe(status_code=200 if accepted else 401, resource_metadata_url=None)

    probe = AsyncMock(side_effect=probe_impl)
    runtime = dataclasses.replace(_runtime(db_session_factory, mcp=True), mcp_token_probe=probe)
    fake, store = TeamsApiFake(), AsyncMock()
    with patch.object(module, "connect_mcp_server_with_token", store):
        async with _running(fake, runtime) as (service, dispatch):
            await post_activity(service, _submit(row.token))
            await service.turns.drain(5)
            store.assert_not_awaited()
            dispatch.assert_not_awaited()
            assert "That token was rejected" in _edits(fake)[-1]
            if failure == "slow_rejection":
                assert any("Still saving" in edit for edit in _edits(fake))
            async with db_session_factory() as session:
                retry = await peek_credential_request(session, token=row.token)
            assert retry is not None and retry.used_at is None and retry.outcome == "token_rejected"
            accepted = True
            monkeypatch.setattr(credential_submit, "CREDENTIAL_CARD_DEADLINE_SECONDS", 90.0)
            await post_activity(service, _submit(row.token))
            await service.turns.drain(5)

    store.assert_awaited_once()
    dispatch.assert_awaited_once()
    assert "✅" in _edits(fake)[-1]


async def test_an_mcp_token_is_stored_attached_and_resumes_the_work(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id, kind="mcp")
    fake, connect = TeamsApiFake(), AsyncMock()
    with patch.object(module, "connect_mcp_server_with_token", connect):
        async with _running(fake, _runtime(db_session_factory, mcp=True)) as (service, dispatch):
            await post_activity(service, _submit(row.token))
            await service.turns.drain(5)

    assert connect.await_args is not None
    assert connect.await_args.kwargs["token"] == SECRET
    assert connect.await_args.kwargs["mcp_server_url"] == MCP_URL
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


async def test_a_member_cannot_replace_an_mcp_server_on_a_shared_agent(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """H2 on Teams: an `mcp_replace` refusal spends the request and publishes nothing."""
    from daimon.core.mcp_attach import McpConnectDecision

    row = await _request(db_session_factory, account_id, kind="mcp")
    fake, connect = TeamsApiFake(), AsyncMock()
    refused = AsyncMock(return_value=McpConnectDecision(replaces=True, replace_allowed=False))
    with (
        patch.object(module, "decide_mcp_connect", refused),
        patch.object(module, "connect_mcp_server_with_token", connect),
    ):
        async with _running(fake, _runtime(db_session_factory, mcp=True)) as (service, dispatch):
            await post_activity(service, _submit(row.token))
            await service.turns.drain(5)

    connect.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert refused.await_args is not None, "the submit decided the replacement"
    assert refused.await_args.kwargs["caller"].is_server_admin is False, "as a member"
    assert "was not replaced" in _edits(fake)[-1]


async def _hold(db: async_sessionmaker[AsyncSession], key: str, account_id: uuid.UUID) -> None:
    async with db.begin() as session:
        await put_agent_file(
            session,
            tenant_id=TENANT,
            agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id=MA_ID),
            key=key,
            content="the-value-in-use",
            set_by_account_id=account_id,
        )


async def test_a_member_cannot_add_an_alias_of_a_held_key_on_a_managed_agent(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """Minted with no key held (no replacement promised); GH_TOKEN is held by submit time."""
    row = await _request(db_session_factory, account_id, target="GITHUB_TOKEN")
    await _hold(db_session_factory, "GH_TOKEN", account_id)
    fake = TeamsApiFake()
    async with _running(fake, _runtime(db_session_factory, managed=True)) as (service, dispatch):
        await post_activity(service, _submit(row.token))
        await service.turns.drain(5)

    async with db_session_factory() as session:
        alias = await get_agent_file(
            session, tenant_id=TENANT, agent_id=row.agent_id, key="GITHUB_TOKEN"
        )
        spent = await peek_credential_request(session, token=row.token)
    assert alias is None, "an alias that retargets a held key is refused like an overwrite"
    assert spent is not None and spent.outcome == "write_failed"
    assert "Adding GITHUB_TOKEN would replace GH_TOKEN" in _edits(fake)[-1], "names both keys"
    dispatch.assert_not_awaited()


async def test_an_alias_that_appears_after_the_gate_is_caught_under_the_write(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    row = await _request(db_session_factory, account_id, target="GITHUB_TOKEN")
    real = credential_submit.list_turn_key_names
    calls = 0

    async def first_read_misses_the_alias(session: AsyncSession, **kw: Any) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ()
        await put_agent_file(
            session,
            tenant_id=TENANT,
            agent_id=row.agent_id,
            key="GH_TOKEN",
            content="the-value-in-use",
            set_by_account_id=None,
        )
        return await real(session, **kw)

    with patch.object(credential_submit, "list_turn_key_names", first_read_misses_the_alias):
        async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, _):
            await post_activity(service, _submit(row.token))
            await service.turns.drain(5)

    assert calls == 2, "the alias must be re-read under the write"
    async with db_session_factory() as session:
        alias = await get_agent_file(
            session, tenant_id=TENANT, agent_id=row.agent_id, key="GITHUB_TOKEN"
        )
        spent = await peek_credential_request(session, token=row.token)
    assert alias is None and spent is not None and spent.outcome == "stale_replacement"


async def test_concurrent_submits_of_two_alias_names_store_only_one(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    """GH_TOKEN and GITHUB_TOKEN submitted at once: the key-set lock lets one win.

    Separate connections, so the two writes really are concurrent. The patched
    insert dawdles, so without the lock the second writer reads "neither held"
    while the first is still mid-write, and both land.
    """
    import asyncio

    from daimon.core.defaults.provisioning import provision_tenant
    from daimon.core.stores.agent_files import list_agent_files

    committing = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    await provision_tenant(committing, platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with committing.begin() as session:
        tenant = await get_tenant(session, TENANT)
        assert tenant is not None
        account = (await make_account(session, tenant=tenant)).id
    first = await _request(committing, account, target="GH_TOKEN")
    second = await _request(committing, account, target="GITHUB_TOKEN")
    real = credential_submit.put_agent_file_if_unchanged

    async def slow_insert(session: AsyncSession, **kw: Any) -> Any:
        await asyncio.sleep(0.3)
        return await real(session, **kw)

    with patch.object(credential_submit, "put_agent_file_if_unchanged", slow_insert):
        async with _running(TeamsApiFake(), _runtime(committing)) as (service, _):
            await asyncio.gather(
                post_activity(service, _submit(first.token)),
                post_activity(service, _submit(second.token)),
            )
            await service.turns.drain(5)

    async with committing() as session:
        rows = await list_agent_files(session, tenant_id=TENANT, agent_id=first.agent_id)
        outcomes = []
        for token in (first.token, second.token):
            spent = await peek_credential_request(session, token=token)
            assert spent is not None
            outcomes.append(spent.outcome)
    assert len(rows) == 1, f"exactly one alias may land, got {[r.key for r in rows]}"
    assert sorted(o or "" for o in outcomes) == ["applied", "stale_replacement"]


async def test_a_teams_submit_waits_for_another_adapters_alias_write(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    """Cross-writer: another adapter holds the key-set lock while adding GH_TOKEN.

    The Teams submit for GITHUB_TOKEN must wait for that write and then see it,
    not read "neither held" from under an uncommitted insert.
    """
    import asyncio

    from daimon.core.defaults.provisioning import provision_tenant
    from daimon.core.stores.agent_files import list_agent_files, lock_agent_keys

    committing = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    await provision_tenant(committing, platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with committing.begin() as session:
        tenant = await get_tenant(session, TENANT)
        assert tenant is not None
        account = (await make_account(session, tenant=tenant)).id
    row = await _request(committing, account, target="GITHUB_TOKEN")
    holding = asyncio.Event()

    async def other_adapter_adds_gh_token() -> None:
        async with committing.begin() as session:
            await lock_agent_keys(session, tenant_id=TENANT, agent_id=row.agent_id)
            await put_agent_file(
                session,
                tenant_id=TENANT,
                agent_id=row.agent_id,
                key="GH_TOKEN",
                content="the-value-in-use",
                set_by_account_id=account,
            )
            holding.set()
            await asyncio.sleep(0.5)

    async with _running(TeamsApiFake(), _runtime(committing)) as (service, _):
        other = asyncio.create_task(other_adapter_adds_gh_token())
        await holding.wait()
        await post_activity(service, _submit(row.token))
        await service.turns.drain(5)
        await other

    async with committing() as session:
        rows = await list_agent_files(session, tenant_id=TENANT, agent_id=row.agent_id)
        spent = await peek_credential_request(session, token=row.token)
    assert [r.key for r in rows] == ["GH_TOKEN"], "the Teams alias must not land beside it"
    assert spent is not None and spent.outcome == "stale_replacement"


async def _aws_pair(db: async_sessionmaker[AsyncSession], account_id: uuid.UUID) -> datetime:
    """Store an AWS access key and secret; return the secret's `updated_at`."""
    agent_id = derive_agent_uuid(tenant_id=TENANT, ma_agent_id=MA_ID)
    async with db.begin() as session:
        await put_agent_file(
            session,
            tenant_id=TENANT,
            agent_id=agent_id,
            key="AWS_ACCESS_KEY_ID",
            content="AKIAEXAMPLE",
            set_by_account_id=account_id,
        )
        secret = await put_agent_file(
            session,
            tenant_id=TENANT,
            agent_id=agent_id,
            key="AWS_SECRET_ACCESS_KEY",
            content="old-secret",
            set_by_account_id=account_id,
        )
    return secret.updated_at


def _admin_runtime(db: async_sessionmaker[AsyncSession], **kw: Any) -> TeamsRuntime:
    from .conftest import teams_settings

    runtime = _runtime(db, **kw)
    runtime.settings.teams = teams_settings(admins=(AAD_OBJECT_ID,))
    return runtime


async def test_an_admin_can_rotate_one_key_of_a_stored_aws_family(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """The stored AWS_ACCESS_KEY_ID is a family member, not a newly appeared conflict."""
    stamp = await _aws_pair(db_session_factory, account_id)
    row = await _request(
        db_session_factory, account_id, target="AWS_SECRET_ACCESS_KEY", replaces=stamp
    )
    async with _running(TeamsApiFake(), _admin_runtime(db_session_factory)) as (service, _):
        await post_activity(service, _submit(row.token, secret="new-secret"))
        await service.turns.drain(5)

    async with db_session_factory() as session:
        file = await get_agent_file(
            session, tenant_id=TENANT, agent_id=row.agent_id, key="AWS_SECRET_ACCESS_KEY"
        )
        spent = await peek_credential_request(session, token=row.token)
    assert file is not None and file.content == "new-secret", "the rotation lands"
    assert spent is not None and spent.outcome == "applied"


async def test_a_family_change_during_an_admin_rotation_is_still_refused(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """A session token grafted on between the gate and the write: the rotation is stale."""
    stamp = await _aws_pair(db_session_factory, account_id)
    row = await _request(
        db_session_factory, account_id, target="AWS_SECRET_ACCESS_KEY", replaces=stamp
    )
    real = credential_submit.list_turn_key_names
    calls = 0

    async def family_changes_after_the_gate(session: AsyncSession, **kw: Any) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls == 2:
            await put_agent_file(
                session,
                tenant_id=TENANT,
                agent_id=row.agent_id,
                key="AWS_SESSION_TOKEN",
                content="grafted",
                set_by_account_id=None,
            )
        return await real(session, **kw)

    with patch.object(credential_submit, "list_turn_key_names", family_changes_after_the_gate):
        async with _running(TeamsApiFake(), _admin_runtime(db_session_factory)) as (service, _):
            await post_activity(service, _submit(row.token, secret="new-secret"))
            await service.turns.drain(5)

    async with db_session_factory() as session:
        file = await get_agent_file(
            session, tenant_id=TENANT, agent_id=row.agent_id, key="AWS_SECRET_ACCESS_KEY"
        )
        spent = await peek_credential_request(session, token=row.token)
    assert file is not None and file.content == "old-secret"
    assert spent is not None and spent.outcome == "stale_replacement"


async def test_a_pinned_agents_form_from_outside_its_channels_is_refused_at_submit(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """A pin added after the card was posted still holds when the value is submitted."""
    row = await _request(db_session_factory, account_id)
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("19:other-channel",)}),
        )
    async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, dispatch):
        refused = await post_activity(service, _submit(row.token))
        await service.turns.drain(5)
    assert refused["task"]["value"] == PIN_WRITE_REFUSAL
    async with db_session_factory() as session:
        file = await get_agent_file(session, tenant_id=TENANT, agent_id=row.agent_id, key="API_KEY")
    assert file is None, "nothing is saved"
    dispatch.assert_not_awaited()


async def _early_check_passes(*_args: object, **_kwargs: object) -> None:
    """The pin lands after the submit path's early check: only the consume sees it."""
    return None


@pytest.mark.parametrize("kind", ["env", "mcp_oauth"])
async def test_a_pin_after_the_early_check_is_decided_inside_the_consume(
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    row = await _request(db_session_factory, account_id, kind=kind)
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("19:other-channel",)}),
        )
    monkeypatch.setattr(
        "daimon.adapters.teams.credential_requests.request_pin_refusal", _early_check_passes
    )
    runtime = _runtime(db_session_factory, mcp=kind == "mcp_oauth")
    async with _running(TeamsApiFake(), runtime) as (service, dispatch):
        answer = await post_activity(
            service, _open(row.token) if kind == "mcp_oauth" else _submit(row.token)
        )
        await service.turns.drain(5)
    if kind == "mcp_oauth":
        assert answer["task"]["value"] == PIN_WRITE_REFUSAL
    async with db_session_factory() as session:
        live = await peek_credential_request(session, token=row.token)
        file = await get_agent_file(session, tenant_id=TENANT, agent_id=row.agent_id, key="API_KEY")
    assert live is not None and live.used_at is None, "the refused form was not spent"
    assert file is None, "nothing is saved"
    dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    ("kind", "multiline"),
    [("env", True), ("env_file", True), ("mcp", False), ("repo", False), ("skill_repo", False)],
)
async def test_each_kind_opens_one_input_sized_for_what_it_takes(
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    kind: str,
    multiline: bool,
) -> None:
    """Values and pasted files can span lines; tokens are masked."""
    row = await _request(db_session_factory, account_id, kind=kind)
    # An admin: a member is refused a repo bind on the shared default agent.
    async with _running(TeamsApiFake(), _admin_runtime(db_session_factory)) as (service, _):
        field = _field(await post_activity(service, _open(row.token)))
    assert bool(field.get("isMultiline")) is multiline, "values and files can span lines"
    assert (field.get("style") == "Password") is not multiline, "a token is never shown"


async def test_a_multi_line_env_value_is_stored_as_typed(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """Line breaks in a pasted value survive the save."""
    row = await _request(db_session_factory, account_id)
    pem = "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"
    async with _running(TeamsApiFake(), _runtime(db_session_factory)) as (service, _):
        await post_activity(service, _submit(row.token, secret=pem))
        await service.turns.drain(5)
    async with db_session_factory() as session:
        file = await get_agent_file(session, tenant_id=TENANT, agent_id=row.agent_id, key="API_KEY")
    assert file is not None and file.content == pem, "stored with its line breaks"


async def _keys(db: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    agent_id = derive_agent_uuid(tenant_id=TENANT, ma_agent_id=MA_ID)
    async with db() as session:
        files = await list_agent_files(session, tenant_id=TENANT, agent_id=agent_id)
    return {file.key: file.content for file in files}


async def test_a_pasted_env_file_saves_every_key_once_and_resumes_the_work(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """A pasted file lands every key and spends the request once."""
    row = await _request(db_session_factory, account_id, kind="env_file")
    fake = TeamsApiFake()
    body = "ALPHA_KEY=alpha-private\nBETA_TOKEN=beta-private\n"
    with structlog.testing.capture_logs() as logs:
        async with _running(fake, _runtime(db_session_factory)) as (service, dispatch):
            bad = await post_activity(service, _submit(row.token, secret="NOT A KEY LINE\n"))
            await service.turns.drain(5)
            untouched = _edits(fake)
            saved = await post_activity(service, _submit(row.token, secret=body))
            await service.turns.drain(5)

    assert "No keys were saved" in json.dumps(bad) and not untouched, "form open, nothing spent"
    assert "Paste a corrected file" in json.dumps(bad), "a dialog takes a paste, not an upload"
    assert not (saved or {}).get("task"), "the dialog closes"
    assert await _keys(db_session_factory) == {
        "ALPHA_KEY": "alpha-private",
        "BETA_TOKEN": "beta-private",
    }
    async with db_session_factory() as session:
        spent = await peek_credential_request(session, token=row.token)
    assert spent is not None and spent.outcome == "applied", "spent once, applied"
    assert "✅" in _edits(fake)[-1], "the card says it landed"
    dispatch.assert_awaited_once_with(TENANT, CONVERSATION_ID, SERVICE_URL)
    sent = json.dumps([r.body for r in fake.requests]) + repr(logs)
    assert "private" not in sent, "no value reaches Teams or the logs"


async def test_a_pasted_env_file_holding_a_stored_key_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """Slack's rule: a file that would replace a key writes nothing."""
    await _hold(db_session_factory, "ALPHA_KEY", account_id)
    row = await _request(db_session_factory, account_id, kind="env_file")
    fake = TeamsApiFake()
    body = "ALPHA_KEY=new\nBETA_TOKEN=beta\n"
    async with _running(fake, _runtime(db_session_factory)) as (service, dispatch):
        await post_activity(service, _submit(row.token, secret=body))
        await service.turns.drain(5)

    assert await _keys(db_session_factory) == {"ALPHA_KEY": "the-value-in-use"}, "whole-file"
    async with db_session_factory() as session:
        spent = await peek_credential_request(session, token=row.token)
    assert spent is not None and spent.outcome == "stale_replacement", "spent, nothing written"
    assert "ALPHA_KEY is already set" in _edits(fake)[-1], "the card names the held key"
    dispatch.assert_not_awaited()


async def test_a_repo_token_is_checked_then_binds_the_working_repo(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """The token is checked against the repo before the bind."""
    row = await _request(db_session_factory, account_id, kind="repo")
    fake = TeamsApiFake()
    access = AsyncMock(side_effect=[False, True])
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(module, "pat_can_access_repo", access),
    ):
        async with _running(fake, _admin_runtime(db_session_factory)) as (service, dispatch):
            bad = await post_activity(service, _submit(row.token, secret="ghp_wrong"))
            await service.turns.drain(5)
            untouched = _edits(fake)
            saved = await post_activity(service, _submit(row.token, secret=" ghp_right \n"))
            await service.turns.drain(5)

    assert "cannot read o/r" in json.dumps(bad), "the form stays open"
    assert untouched and "That token was rejected" in untouched[-1]
    assert not (saved or {}).get("task"), "the dialog closes"
    assert access.await_args is not None, "the token was checked"
    assert access.await_args.kwargs["pat"] == "ghp_right", "checked as stored: stripped"
    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=TENANT, agent_id=row.agent_id)
        spent = await peek_credential_request(session, token=row.token)
    assert binding is not None and binding.repo_url == "o/r", "the working repo is bound"
    assert binding.default_branch == "release", "the branch the card was posted for"
    assert binding.ma_secret_ref == f"inline-pat:{row.agent_id}", "the agent's own token"
    assert binding.proof_kind == "pat", "proved by the token check"
    assert spent is not None and spent.outcome == "applied", "spent once, applied"
    assert "✅" in _edits(fake)[-1], "the card says it landed"
    dispatch.assert_awaited_once_with(TENANT, CONVERSATION_ID, SERVICE_URL)
    sent = json.dumps([r.body for r in fake.requests]) + repr(logs)
    assert "ghp_right" not in sent and "ghp_wrong" not in sent, "no token in Teams or logs"


@pytest.mark.parametrize("kind", ["repo", "skill_repo"])
async def test_a_member_cannot_give_a_managed_agent_a_repo_or_skills(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID, kind: str
) -> None:
    """Decided before the consume, as on Slack: nothing spent, the card says why."""
    row = await _request(db_session_factory, account_id, kind=kind)
    fake, access, sync = TeamsApiFake(), AsyncMock(return_value=True), AsyncMock()
    with (
        patch.object(module, "pat_can_access_repo", access),
        patch.object(module, "run_skill_sync", sync),
    ):
        async with _running(fake, _runtime(db_session_factory, managed=True)) as (service, _):
            refused = await post_activity(service, _submit(row.token, secret="ghp_x"))
            await service.turns.drain(5)

    expected = module._SHARED_AGENT_SKILLS if kind == "skill_repo" else module._SHARED_AGENT  # pyright: ignore[reportPrivateUsage]
    assert refused["task"]["value"] == expected, "the shared-agent refusal"
    access.assert_not_awaited()
    sync.assert_not_awaited()
    async with db_session_factory() as session:
        live = await peek_credential_request(session, token=row.token)
        binding = await get_binding(session, tenant_id=TENANT, agent_id=row.agent_id)
    assert live is not None and live.used_at is None, "the request is not spent"
    assert binding is None, "nothing bound"
    assert "working repo was not changed" in _edits(fake)[-1], "the card says why"


def _imported() -> AsyncMock:
    skill = ResourceOutcome(
        kind="skill", name="eda", action=Action.CREATED, anthropic_id="skill_01eda"
    )
    return AsyncMock(return_value=[skill])


@pytest.mark.parametrize("slow", [False, True])
async def test_a_skill_repo_token_imports_and_attaches_without_binding_the_repo(
    db_session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    slow: bool,
) -> None:
    """Imported skills attach to the agent; the working repo is untouched."""
    row = await _request(db_session_factory, account_id, kind="skill_repo")
    fake, sync, updates = TeamsApiFake(), _imported(), list[dict[str, Any]]()
    started, finish = asyncio.Event(), asyncio.Event()
    imported = sync.return_value

    async def import_skills(*args: Any, **kwargs: Any) -> list[ResourceOutcome]:
        started.set()
        if slow:
            await finish.wait()
        return imported

    sync.side_effect = import_skills
    if slow:
        monkeypatch.setattr(credential_submit, "CREDENTIAL_CARD_DEADLINE_SECONDS", 0.01)
    runtime = _admin_runtime(db_session_factory, updates=updates)
    with (
        patch.object(module, "pat_can_access_repo", AsyncMock(return_value=True)),
        patch.object(module, "run_skill_sync", sync),
    ):
        async with _running(fake, runtime) as (service, dispatch):
            await post_activity(service, _submit(row.token, secret="ghp_skills"))
            try:
                if slow:
                    async with asyncio.timeout(10):
                        await started.wait()
                        while not any("Still saving" in edit for edit in _edits(fake)):
                            await asyncio.sleep(0.01)
                    async with db_session_factory() as session:
                        held = await peek_credential_request(session, token=row.token)
                    assert held is not None and held.used_at is not None and held.outcome is None
                    assert "ghp_skills" not in _edits(fake)[-1]
                    assert not updates, "the slow import is still running, not cancelled"
                    dispatch.assert_not_awaited()
            finally:
                finish.set()
                await service.turns.drain(5)

    assert sync.await_args is not None, "the import ran"
    where = (sync.await_args.kwargs["branch"], sync.await_args.kwargs["path"])
    assert where == ("main", "skills"), "from the branch and path the card was posted for"
    assert sync.await_args.kwargs["is_admin"] is True, "under the submitter's live role"
    assert {s["skill_id"] for s in updates[-1]["skills"]} >= {"skill_01eda"}, "attached"
    async with db_session_factory() as session:
        binding = await get_binding(session, tenant_id=TENANT, agent_id=row.agent_id)
        credential = await get_skill_repo_credential(
            session,
            tenant_id=TENANT,
            agent_id=row.agent_id,
            repo_url="https://github.com/o/skills",
        )
    assert binding is None, "the working repo does not change"
    assert credential is not None and credential.proof_kind == "pat", "the skill repo token"
    assert "✅" in _edits(fake)[-1], "the card says it landed"
    dispatch.assert_awaited_once_with(TENANT, CONVERSATION_ID, SERVICE_URL)


async def test_a_skill_import_that_lands_nothing_is_not_reported_as_applied(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """An empty import ends partial, never applied."""
    row = await _request(db_session_factory, account_id, kind="skill_repo")
    fake, updates = TeamsApiFake(), list[dict[str, Any]]()
    with (
        patch.object(module, "pat_can_access_repo", AsyncMock(return_value=True)),
        patch.object(module, "run_skill_sync", AsyncMock(return_value=[])),
    ):
        runtime = _admin_runtime(db_session_factory, updates=updates)
        async with _running(fake, runtime) as (service, d):
            await post_activity(service, _submit(row.token, secret="ghp_skills"))
            await service.turns.drain(5)

    async with db_session_factory() as session:
        spent = await peek_credential_request(session, token=row.token)
    assert spent is not None and spent.outcome == "write_failed", "spent, not applied"
    assert not updates, "nothing to attach"
    assert "✅" not in _edits(fake)[-1], "the card does not claim it landed"
    d.assert_not_awaited()


@pytest.mark.parametrize("kind", ["repo", "skill_repo"])
async def test_a_member_is_not_asked_for_a_repo_token_a_shared_agent_refuses(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID, kind: str
) -> None:
    """Slack gates a repo bind at the click, a skill import only at the submit."""
    row = await _request(db_session_factory, account_id, kind=kind)
    fake = TeamsApiFake()
    async with _running(fake, _runtime(db_session_factory, managed=True)) as (service, _):
        opened = await post_activity(service, _open(row.token))
    if kind == "repo":
        assert opened["task"]["value"] == module._SHARED_AGENT, "no form for a refused bind"  # pyright: ignore[reportPrivateUsage]
    else:
        assert "Skill repo only" in json.dumps(opened, ensure_ascii=False), "as Slack's form"
    async with db_session_factory() as session:
        live = await peek_credential_request(session, token=row.token)
    assert live is not None and live.used_at is None, "a click spends nothing"
    assert not _edits(fake), "the card stays as posted"


@pytest.mark.parametrize("kind", ["repo", "skill_repo"])
async def test_a_github_token_needs_encryption_keys(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID, kind: str
) -> None:
    """Without keys the token would be stored plain, so the form refuses it."""
    row = await _request(db_session_factory, account_id, kind=kind)
    runtime = _admin_runtime(db_session_factory)
    runtime = dataclasses.replace(
        runtime, turn_deps=dataclasses.replace(runtime.turn_deps, fernet=None)
    )
    async with _running(TeamsApiFake(), runtime) as (service, _):
        refused = await post_activity(service, _submit(row.token, secret="ghp_x"))
    assert refused["task"]["value"] == module._UNCONFIGURED, "not set up"  # pyright: ignore[reportPrivateUsage]
    async with db_session_factory() as session:
        live = await peek_credential_request(session, token=row.token)
    assert live is not None and live.used_at is None, "nothing spent"


async def test_a_skill_repo_token_that_cannot_read_the_repo_spends_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """Checked before the consume, as the working repo's token is."""
    row = await _request(db_session_factory, account_id, kind="skill_repo")
    fake, sync = TeamsApiFake(), AsyncMock()
    with (
        patch.object(module, "pat_can_access_repo", AsyncMock(return_value=False)),
        patch.object(module, "run_skill_sync", sync),
    ):
        async with _running(fake, _admin_runtime(db_session_factory)) as (service, _):
            bad = await post_activity(service, _submit(row.token, secret="ghp_wrong"))
            await service.turns.drain(5)
    assert "cannot read o/skills" in json.dumps(bad), "the form stays open with why"
    sync.assert_not_awaited()
    assert "That token was rejected" in _edits(fake)[-1]
    async with db_session_factory() as session:
        retry = await peek_credential_request(session, token=row.token)
    assert retry is not None and retry.used_at is None and retry.outcome == "token_rejected"


async def test_a_failed_bind_spends_the_request_and_says_so_without_the_token(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    """A write error can quote its parameters, so only its type is logged."""
    row = await _request(db_session_factory, account_id, kind="repo")
    fake = TeamsApiFake()
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(module, "pat_can_access_repo", AsyncMock(return_value=True)),
        patch.object(module, "set_binding", AsyncMock(side_effect=RuntimeError("ghp_leak"))),
    ):
        async with _running(fake, _admin_runtime(db_session_factory)) as (service, dispatch):
            await post_activity(service, _submit(row.token, secret="ghp_leak"))
            await service.turns.drain(5)
    async with db_session_factory() as session:
        spent = await peek_credential_request(session, token=row.token)
    assert spent is not None and spent.outcome == "write_failed", "spent, not applied"
    assert "✅" not in _edits(fake)[-1], "the card does not claim it landed"
    assert "repo_bound" not in repr(logs), "nothing is logged as bound"
    sent = json.dumps([r.body for r in fake.requests]) + repr(logs)
    assert "ghp_leak" not in sent, "no token in Teams or logs"
    dispatch.assert_not_awaited()


def _from_another_organisation(
    payload: dict[str, object], tenant: str = str(uuid.UUID(int=99))
) -> dict[str, object]:
    cast(dict[str, object], payload["conversation"])["conversationType"] = "channel"
    payload["channelData"] = {"tenant": {"id": tenant}}
    return payload


async def test_an_external_participant_may_only_open_a_sign_in(
    db_session_factory: async_sessionmaker[AsyncSession], account_id: uuid.UUID
) -> None:
    env = await _request(db_session_factory, account_id)
    oauth = await _request(db_session_factory, account_id, kind="mcp_oauth")
    async with _running(TeamsApiFake(), _runtime(db_session_factory, mcp=True)) as (service, _):
        refused = await post_activity(service, _from_another_organisation(_open(env.token)))
        ours = await post_activity(
            service, _from_another_organisation(_open(env.token), ENTRA_TENANT_ID)
        )
        link = await post_activity(service, _from_another_organisation(_open(oauth.token)))
        await service.turns.drain(5)

    assert refused["task"]["value"] == NO_LONGER_VALID_MESSAGE, "no key or token form"
    assert "value" not in _field(ours), "the same click from our own tenant gets the form"
    (button,) = link["task"]["value"]["card"]["content"]["actions"]
    assert button["type"] == "Action.OpenUrl", "their own sign-in"

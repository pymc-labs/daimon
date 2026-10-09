"""Scheduler retirement and refresh call sites preserve transport and ordering."""

# pyright: reportPrivateUsage=false
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import httpx
import pytest
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.scheduler import main
from daimon.core.config import Settings
from daimon.core.github_app_session import archive_app_vault
from daimon.core.mux_compat import archive_session
from daimon.core.session_ports_compat import retrieve_session_record
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores import github_issued_tokens as tokens
from daimon.core.stores.domain import ThreadSessionRow
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = UUID(int=7)
ACCOUNT = UUID(int=8)
AGENT = UUID(int=9)
NOW = datetime.now(UTC)


class Transaction:
    async def __aenter__(self) -> "Transaction":
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass


class Sessionmaker:
    def __call__(self) -> Transaction:
        return Transaction()

    def begin(self) -> Transaction:
        return Transaction()


class TrackingTransport(ScriptedTransport):
    def __init__(self, effects: list[str]) -> None:
        super().__init__()
        self.effects = effects

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        self.effects.append(request.url.path)
        return super().dispatch(request)


def mapped() -> tokens.LiveAppSession:
    snapshot = SessionSnapshot(
        ma_agent_id="agent1",
        model_id="model",
        system_sha256=None,
        skills_sha256="skills",
        environment_id="env1",
        github_mode="app",
        repo_urls=("old",),
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id="vault1",
        tools_sha256="tools",
        mcp_servers_sha256="servers",
        env_sha256=None,
        agent_version=1,
        agent_name="agent",
    )
    return tokens.LiveAppSession(
        mapping=ThreadSessionRow(
            id=UUID(int=10),
            tenant_id=TENANT,
            platform="cli",
            thread_id="thread1",
            account_id=ACCOUNT,
            ma_session_id="session1",
            watermark_message_id=None,
            status="live",
            created_at=NOW,
            updated_at=NOW,
            effective_config=snapshot,
        ),
        agent_id=AGENT,
        expires_at=NOW,
        has_linked_requester=True,
        permissions_by_repo={},
    )


def mcp(*, erased: bool = False) -> tokens.LiveMcpAppSession:
    return tokens.LiveMcpAppSession(
        session_id="session1",
        tenant_id=TENANT,
        vault_id="vault1",
        agent_id=AGENT,
        account_id=None if erased else ACCOUNT,
        repo_urls=("old",),
        repo_resource_ids={},
        expires_at=NOW,
        permissions_by_repo={},
        last_started_at=NOW,
    )


@pytest.mark.parametrize(
    "kind,status",
    [
        ("retire", 200),
        ("retire", 403),
        ("retire", 404),
        ("retire", 409),
        ("mapped", 200),
        ("mapped", 403),
        ("refresh", 200),
        ("refresh", 404),
        ("close", 200),
        ("close", 404),
        ("close", 403),
        ("running", 200),
    ],
)
async def test_scheduler_actual_callsite_requests_scopes_and_cleanup_order(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    status: int,
) -> None:
    effects: list[str] = []
    before, after = ScriptedTransport(), TrackingTransport(effects)
    read = kind in ("refresh", "close", "running")
    archive = kind in ("retire", "mapped") or (kind == "refresh" and status == 200)
    vault = (archive and status == 200) or (kind == "close" and status in (200, 404))
    for transport in (before, after):
        if read:
            transport.queue(
                ScriptedReply(
                    "GET",
                    "/v1/sessions/session1",
                    httpx.Response(
                        status,
                        json={
                            "id": "session1",
                            "status": "running" if kind == "running" else "idle",
                            "metadata": {"daimon_tenant": str(TENANT)},
                        }
                        if status == 200
                        else {"error": {"type": "not_found_error", "message": "refused"}},
                    ),
                )
            )
        if archive:
            transport.queue(
                ScriptedReply(
                    "POST",
                    "/v1/sessions/session1/archive",
                    httpx.Response(
                        status,
                        json={}
                        if status == 200
                        else {"error": {"type": "permission_error", "message": "refused"}},
                    ),
                )
            )
        if vault:
            transport.queue(
                ScriptedReply("POST", "/v1/vaults/vault1/archive", httpx.Response(200, json={}))
            )
    scopes: list[Scope] = []

    async def scoped_archive(client: AsyncAnthropic, session_id: str, *, scope: Scope) -> None:
        scopes.append(scope)
        await archive_session(client, session_id, scope=scope)

    async def scoped_read(client: AsyncAnthropic, session_id: str, *, scope: Scope):
        scopes.append(scope)
        return await retrieve_session_record(client, session_id, scope=scope)

    async def scoped_vault(client: AsyncAnthropic, *, vault_id: str, scope: Scope) -> None:
        scopes.append(scope)
        await archive_app_vault(client, vault_id=vault_id, scope=scope)

    @asynccontextmanager
    async def fence(*args: object, **kwargs: object) -> AsyncIterator[None]:
        yield None

    async def live(*args: object, **kwargs: object) -> list[tokens.LiveAppSession]:
        return [mapped()] if kind == "mapped" else []

    async def live_mcp(*args: object, **kwargs: object) -> list[tokens.LiveMcpAppSession]:
        return [mcp(erased=True)] if kind == "refresh" else []

    closed = tokens.ClosedAppSession(
        session_id="session1", vault_id="vault1", is_mcp=True, tenant_id=TENANT, account_id=ACCOUNT
    )

    async def closed_list(*args: object, **kwargs: object) -> list[tokens.ClosedAppSession]:
        return [closed]

    async def current(*args: object, **kwargs: object) -> tokens.ClosedAppSession:
        return closed

    async def desired(
        *args: object, **kwargs: object
    ) -> tuple[tuple[str, ...], dict[int, dict[str, str]]]:
        return ("new",), {}

    def effect(name: str):
        async def record(*args: object, **kwargs: object) -> None:
            effects.append(name)

        return record

    monkeypatch.setattr(main, "archive_session", scoped_archive)
    monkeypatch.setattr(main, "retrieve_session_record", scoped_read)
    monkeypatch.setattr(main, "archive_app_vault", scoped_vault)
    monkeypatch.setattr(main, "session_mutation_fence", fence)
    monkeypatch.setattr(main, "list_live_app_sessions", live)
    monkeypatch.setattr(main, "list_live_mcp_app_sessions", live_mcp)
    monkeypatch.setattr(main, "list_closed_app_sessions", closed_list)
    monkeypatch.setattr(main, "closed_app_session_for_id", current)
    monkeypatch.setattr(main, "effective_repo_state", desired)
    for name, marker in (
        ("finish_headless_app_session", "finish"),
        ("revoke_session_tokens", "revoke"),
        ("mark_headless_app_session_closed", "closed"),
        ("mark_dead", "dead"),
        ("touch_running_mcp_app_session", "touch"),
    ):
        monkeypatch.setattr(main, name, effect(marker))
    monkeypatch.setattr(main, "_last_app_access_checks", {})
    monkeypatch.setattr(main, "_app_refresh_failures", {})
    monkeypatch.setattr(main, "_mcp_running_since", {})
    sm = cast(async_sessionmaker[AsyncSession], Sessionmaker())
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    old_error: Exception | None = None
    new_error: Exception | None = None
    async with before.client() as old, after.client() as new:
        try:
            if read:
                await old.beta.sessions.retrieve("session1")
            if archive:
                await old.beta.sessions.archive("session1")
        except Exception as exc:
            old_error = exc
        if vault:
            await old.beta.vaults.archive("vault1")
        try:
            if kind == "retire":
                await main._retire_mcp_app_session(new, sm, mcp(), fernet=fernet)
            elif kind in ("mapped", "refresh"):
                await main._refresh_github_app_sessions(
                    new,
                    sm,
                    settings=Settings.model_validate(
                        {
                            "database": {"url": "postgresql+asyncpg://localhost/offline"},
                            "anthropic": {"api_key": "offline"},
                        }
                    ),
                    fernet=fernet,
                )
            else:
                await main._close_github_app_sessions(new, sm, fernet=fernet)
        except Exception as exc:
            new_error = exc
    before.assert_consumed()
    after.assert_consumed()
    assert after.requests == before.requests
    if kind == "retire":
        assert type(new_error) is type(old_error)
        assert str(new_error) == str(old_error)
    else:
        assert new_error is None  # existing sweep owns its refusal/backoff policy
    assert scopes and all(
        s.tenant_id == str(TENANT) and s.platform_reason is None and s.legacy_call_site is None
        for s in scopes
    )
    expected_account = "service" if kind == "refresh" else str(ACCOUNT)
    assert all(s.account_id == expected_account for s in scopes)
    if kind == "retire" and status == 200:
        assert effects == [
            "/v1/sessions/session1/archive",
            "finish",
            "revoke",
            "/v1/vaults/vault1/archive",
            "closed",
        ]
    elif kind == "mapped" and status == 200:
        assert effects == [
            "/v1/sessions/session1/archive",
            "dead",
            "revoke",
            "/v1/vaults/vault1/archive",
        ]
    elif kind == "refresh" and status == 200:
        assert effects == [
            "/v1/sessions/session1",
            "/v1/sessions/session1/archive",
            "finish",
            "revoke",
            "/v1/vaults/vault1/archive",
            "closed",
        ]
    elif kind == "refresh" and status == 404:
        assert effects == ["/v1/sessions/session1", "finish"]
    elif kind == "close" and status in (200, 404):
        assert effects == ["/v1/sessions/session1", "revoke", "/v1/vaults/vault1/archive", "closed"]
    elif kind == "running":
        assert effects == ["/v1/sessions/session1", "touch"]
    else:
        assert effects == [r.path for r in after.requests]


async def test_closed_projection_exposes_the_already_fetched_tenant_and_account(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await tokens.register_app_session_vault(
        db_session,
        session_id="projection-session",
        tenant_id=tenant.id,
        account_id=account.id,
        vault_id="projection-vault",
        is_unmapped=True,
    )
    await tokens.finish_headless_app_session(db_session, session_id="projection-session")
    actual = await tokens.closed_app_session_for_id(
        db_session, session_id="projection-session", now=datetime.now(UTC) + timedelta(minutes=47)
    )
    assert actual is not None
    assert actual.tenant_id == tenant.id
    assert actual.account_id == account.id

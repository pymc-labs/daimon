"""`create_session` chooses the GitHub MCP router only for new multi-token sessions.

The router record, its vault bearer, the re-pointed `github` server, the
session's transport stamp and the record id on every minted token come
together or not at all; any failure leaves the session direct, and a failed
creation closes the record locally first.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import Fernet
from daimon.core._models import GitHubRouterSession
from daimon.core.config import McpSettings
from daimon.core.github_app_session import AppSessionAccess, AppToken
from daimon.core.github_credentials import build_multifernet
from daimon.core.mcp_server_url import GITHUB_COPILOT_MCP_URL, github_router_url
from daimon.core.session_preparation_stages import desired_snapshot_for
from daimon.core.session_snapshot import (
    MA_METADATA_KEY_GITHUB_TRANSPORT,
    SessionSnapshot,
    snapshot_from_created_session,
)
from daimon.core.sessions import create_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import build_fake_anthropic as build_fake_anthropic_http
from mux.testing.drivers.anthropic.ma import json_body
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from pydantic import HttpUrl, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PUBLIC_URL = "https://daimon.example.com/mcp"
_ROUTER_URL = github_router_url(_PUBLIC_URL)
_MCP = McpSettings(jwt_secret=SecretStr("x" * 32), public_url=HttpUrl(_PUBLIC_URL))


def _repo(installation_id: int, repo_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(installation_id=installation_id, repo_id=repo_id, repo_full_name=name)


#: Grant views: (rows, user id, link generation, refused urls).
_TWO_INSTALLATIONS = (
    (
        (SimpleNamespace(is_working_repo=True), _repo(771, 101, "acme/app"), "write"),
        (SimpleNamespace(is_working_repo=False), _repo(772, 201, "client-org/product"), "write"),
    ),
    None,
    None,
    (),
)
_ONE_INSTALLATION_TWO_CHUNKS = (
    (
        (SimpleNamespace(is_working_repo=True), _repo(771, 101, "acme/app"), "write"),
        (SimpleNamespace(is_working_repo=False), _repo(771, 103, "acme/docs"), "read"),
    ),
    None,
    None,
    (),
)
_ONE_TOKEN = (
    (
        (SimpleNamespace(is_working_repo=True), _repo(771, 101, "acme/app"), "write"),
        (SimpleNamespace(is_working_repo=False), _repo(771, 102, "acme/infra"), "write"),
    ),
    None,
    None,
    (),
)
#: Two installations, but GitHub refused the second one's only repo.
_SECOND_INSTALLATION_REFUSED = (
    _TWO_INSTALLATIONS[0],
    None,
    None,
    ("https://github.com/client-org/product",),
)


def _github_agent(*, with_github: bool = True) -> BetaManagedAgentsAgent:
    servers = [{"name": "github", "type": "url", "url": GITHUB_COPILOT_MCP_URL}]
    return ma_agent(
        id="ag_router",
        name="router-agent",
        model="claude-opus-4-7",
        mcp_servers=servers if with_github else [],
    )


class _Harness:
    """`create_session`'s App collaborators faked, MA's session create recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, grant_view: object) -> None:
        tokens = (
            AppToken(uuid.uuid4(), "token-a", 771, (101,), "write", "GH_TOKEN_I771_0", 1, 0),
            AppToken(uuid.uuid4(), "token-b", 772, (201,), "write", "GH_TOKEN_I772_0", 1, 0),
        )
        self.access = AppSessionAccess((), tokens, "token-a", multi_installation=True)
        self.prepare = AsyncMock(return_value=self.access)
        self.write_bearer = AsyncMock()
        self.bodies: list[dict[str, Any]] = []
        for name, value in {
            "effective_app_grant_view": AsyncMock(return_value=grant_view),
            "create_session_vault": AsyncMock(return_value="vlt_app"),
            "prepare_app_access": self.prepare,
            "add_github_router_credential": self.write_bearer,
            "add_app_credentials": AsyncMock(),
            "finish_app_delivery": AsyncMock(),
            "revoke_app_access": AsyncMock(),
            "archive_app_vault": AsyncMock(),
            "resolve_bot_identity": AsyncMock(return_value=None),
            "upload_github_repo_helpers_and_mount": AsyncMock(return_value=()),
            "upload_env_and_mount": AsyncMock(return_value=None),
            "ensure_memory_store_and_mount": AsyncMock(
                return_value={"type": "memory_store", "memory_store_id": "memstore_router"}
            ),
        }.items():
            monkeypatch.setattr(f"daimon.core.sessions.{name}", value)

    def handler(self, *, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/sessions":
                body = json_body(request)
                self.bodies.append(body)
                if status != 200:
                    return httpx.Response(
                        status, json={"type": "error", "error": {"type": "api_error"}}
                    )
                return httpx.Response(
                    200,
                    json=ma_session(
                        id="sesn_router",
                        agent=ma_session_agent(id="ag_router", name="router-agent"),
                        environment_id=body["environment_id"],
                        metadata=body.get("metadata") or {},
                    ).model_dump(mode="json"),
                )
            # Archiving a session that failed after creation.
            return httpx.Response(200, json={})

        return handle

    @property
    def router_session_id(self) -> uuid.UUID | None:
        assert self.prepare.await_args is not None
        return self.prepare.await_args.kwargs["router_session_id"]


async def _create(
    harness: _Harness,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    enabled: bool = True,
    agent: BetaManagedAgentsAgent | None = None,
    mcp: McpSettings | None = _MCP,
    status: int = 200,
) -> None:
    await create_session(
        build_fake_anthropic_http(harness.handler(status=status)),
        agent=agent if agent is not None else _github_agent(),
        environment=ma_environment(id="env_router", name="e"),
        mcp_settings=mcp,
        tenant_id=tenant_id,
        agent_uuid=uuid.uuid4(),
        session_factory=factory,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
        tool_safety=OPEN_TOOL_SAFETY,
        github_mcp_router=enabled,
    )


async def _records(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[GitHubRouterSession]:
    async with factory() as session:
        rows = await session.scalars(
            select(GitHubRouterSession).where(GitHubRouterSession.tenant_id == tenant_id)
        )
        return list(rows)


@pytest.mark.parametrize(
    "grant_view",
    [
        pytest.param(_TWO_INSTALLATIONS, id="two-installations"),
        pytest.param(_ONE_INSTALLATION_TWO_CHUNKS, id="one-installation-two-chunks"),
    ],
)
async def test_a_multi_token_session_is_routed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    grant_view: object,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    harness = _Harness(monkeypatch, grant_view)

    await _create(harness, db_session_factory, tenant.id)

    (record,) = await _records(db_session_factory, tenant.id)
    assert record.closed_at is None
    assert record.ma_session_id == "sesn_router", "bound to the session once it exists"
    assert harness.router_session_id == record.id, "every minted token carries the record"
    assert harness.write_bearer.await_args is not None
    written = harness.write_bearer.await_args.kwargs
    assert written["vault_id"] == "vlt_app" and written["router_url"] == _ROUTER_URL
    assert hashlib.sha256(written["bearer"].encode()).digest() == record.credential_sha256
    (body,) = harness.bodies
    assert body["metadata"][MA_METADATA_KEY_GITHUB_TRANSPORT] == "router"
    assert body["agent"]["mcp_servers"] == [{"name": "github", "type": "url", "url": _ROUTER_URL}]
    assert written["bearer"] not in json.dumps(body), "the bearer only reaches the vault"


@pytest.mark.parametrize(
    ("enabled", "grant_view", "with_github", "mcp"),
    [
        pytest.param(False, _TWO_INSTALLATIONS, True, _MCP, id="setting-off"),
        pytest.param(True, _ONE_TOKEN, True, _MCP, id="one-token"),
        pytest.param(True, _SECOND_INSTALLATION_REFUSED, True, _MCP, id="refused-down-to-one"),
        pytest.param(True, _TWO_INSTALLATIONS, False, _MCP, id="no-github-server"),
        pytest.param(True, _TWO_INSTALLATIONS, True, None, id="no-public-url"),
    ],
)
async def test_other_sessions_stay_direct(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    grant_view: object,
    with_github: bool,
    mcp: McpSettings | None,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    harness = _Harness(monkeypatch, grant_view)

    await _create(
        harness,
        db_session_factory,
        tenant.id,
        enabled=enabled,
        agent=_github_agent(with_github=with_github),
        mcp=mcp,
    )

    assert await _records(db_session_factory, tenant.id) == []
    harness.write_bearer.assert_not_awaited()
    assert harness.router_session_id is None
    (body,) = harness.bodies
    assert MA_METADATA_KEY_GITHUB_TRANSPORT not in (body.get("metadata") or {})
    assert _ROUTER_URL not in json.dumps(body)


async def test_a_failed_bearer_write_closes_the_record_and_stays_direct(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    harness = _Harness(monkeypatch, _TWO_INSTALLATIONS)
    harness.write_bearer.side_effect = RuntimeError("vault unavailable")

    await _create(harness, db_session_factory, tenant.id)

    (record,) = await _records(db_session_factory, tenant.id)
    assert record.closed_at is not None, "the unwritten bearer is revoked locally"
    assert record.ma_session_id is None
    assert harness.router_session_id is None
    (body,) = harness.bodies
    assert MA_METADATA_KEY_GITHUB_TRANSPORT not in body["metadata"]
    assert _ROUTER_URL not in json.dumps(body)


async def test_a_failed_session_create_closes_the_record_locally(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic

    tenant = await make_tenant(db_session)
    await db_session.commit()
    harness = _Harness(monkeypatch, _TWO_INSTALLATIONS)

    with pytest.raises(anthropic.APIError):
        await _create(harness, db_session_factory, tenant.id, status=500)

    (record,) = await _records(db_session_factory, tenant.id)
    assert record.closed_at is not None


async def test_the_desired_session_follows_the_recorded_mode_not_the_setting(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`desired_snapshot_for` takes no router setting: turning it off cannot flip a
    routed session's servers, and turning it on cannot flip a direct one's."""
    agent = _github_agent()
    created = ma_session(
        id="sesn_routed",
        agent=ma_session_agent(
            id="ag_router",
            name="router-agent",
            mcp_servers=[{"name": "github", "type": "url", "url": _ROUTER_URL}],
        ),
        environment_id="env_router",
        metadata={MA_METADATA_KEY_GITHUB_TRANSPORT: "router"},
    )
    recorded = snapshot_from_created_session(
        created,
        env_sha256=None,
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id=None,
        sent_skills=[],
    )

    async def desired(recorded: SessionSnapshot) -> SessionSnapshot:
        return await desired_snapshot_for(
            db_session_factory,
            agent=agent,
            environment_id="env_router",
            tenant_id=uuid.uuid4(),
            agent_uuid=uuid.uuid4(),
            account_id=uuid.uuid4(),
            recorded=recorded,
            tool_safety=OPEN_TOOL_SAFETY,
            public_url=_PUBLIC_URL,
        )

    routed = await desired(recorded)
    direct = await desired(recorded.model_copy(update={"github_transport": "direct"}))
    assert routed.mcp_servers_sha256 == recorded.mcp_servers_sha256
    assert direct.mcp_servers_sha256 != recorded.mcp_servers_sha256

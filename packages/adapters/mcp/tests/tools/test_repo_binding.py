"""Binding a public repo from chat, through real stores and real transports.

The load-bearing properties: only a repo GitHub confirms is public is ever
written with the ``anon:`` ref the operator's fallback token clones; a caller
who may not change a shared agent is refused before GitHub is asked anything
at all; and the confirmation never claims a file copy that has not happened.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.repo_binding import (
    _bind_public_repo_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.domain import AgentRepoBindingRow, Role
from daimon.core.stores.thread_sessions import create_thread_session, get_live_thread_session
from daimon.core.turn_origin import turn_origin
from daimon.testing import ma_agent, ma_model_config
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TARGET_ID = "agt_research"
_TARGET_NAME = "research-bot"
_RESPONDER_ID = "agt_daimon"
_REPO_URL = "https://github.com/acme/data"
_OWNER_REPO = "acme/data"


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic,
    *,
    default_agent_name: str | None = None,
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=client,
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
        ),
        # The deployment default is the cheapest way to make one agent
        # reachable: on a tenant with no config rows it is what every mention
        # resolves to, which is exactly what the admin gate keys off.
        deployment_default=DeploymentDefault(agent_name=default_agent_name),
    )


def _client(tenant_id: uuid.UUID) -> AsyncAnthropic:
    payload = ma_agent(
        id=_TARGET_ID,
        name=_TARGET_NAME,
        model=ma_model_config("claude-sonnet-5", speed="standard"),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: _TARGET_NAME,
        },
    ).model_dump(mode="json")
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([payload]))
    return build_fake_anthropic(router.dispatch)


def _github(*, private: bool = False) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A GitHub API stub plus the log of what it was actually asked.

    The log is what proves a refusal short-circuited before the network, which
    an assertion on the binding row alone cannot distinguish from a refusal
    that happened after paying for the round trip.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"private": private})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _snapshot(*, repo_url: str | None) -> SessionSnapshot:
    return SessionSnapshot(
        ma_agent_id=_RESPONDER_ID,
        model_id="claude-sonnet-5",
        system_sha256=None,
        skills_sha256="skills",
        environment_id="env_1",
        repo_url=repo_url,
        repo_branch=None if repo_url is None else "main",
        memory_store_id=None,
        vault_id=None,
        tools_sha256="tools",
        mcp_servers_sha256="mcp",
        env_sha256=None,
        agent_version=1,
        agent_name="daimon",
    )


def _auth_identity(*, tenant_id: uuid.UUID, account_id: uuid.UUID, is_admin: bool) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.ADMIN if is_admin else Role.USER,
        platform="discord",
        platform_user_id="42",
        is_admin=is_admin,
    )


async def _binding(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID
) -> AgentRepoBindingRow | None:
    async with sessionmaker() as session:
        return await get_binding(
            session,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_TARGET_ID),
        )


async def test_bind_public_repo_writes_a_public_proof_binding(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client(tenant.id))
    auth = _auth_identity(tenant_id=tenant.id, account_id=caller.id, is_admin=False)
    github, seen = _github()

    async with (
        github,
        turn_origin(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=caller.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
            responder_ma_agent_id=_RESPONDER_ID,
            responder_name="daimon",
            role=Role.USER,
        ) as origin,
    ):
        result = await _bind_public_repo_impl(
            runtime,
            auth,
            agent_name=_TARGET_NAME,
            repo_url=_REPO_URL,
            branch="main",
            origin_context_id=str(origin.id),
            expected_ma_agent_id=_TARGET_ID,
            unsaved_work=None,
            http_client=github,
        )

    assert [str(r.url) for r in seen] == [f"https://api.github.com/repos/{_OWNER_REPO}"], (
        "visibility is checked against the same repo the binding stores"
    )
    assert result.repo_url == _OWNER_REPO, "the result reports the canonical form that was stored"
    assert result.branch == "main", "the branch is carried into the binding"
    assert f"{_TARGET_NAME} now works in {_OWNER_REPO} on main." in result.confirmation, (
        "the tool returns final copy the model can relay without composing its own"
    )

    row = await _binding(committing_sessionmaker, tenant_id=tenant.id)
    assert row is not None, "a verified-public repo is bound in the same turn that asked"
    assert (row.repo_url, row.default_branch) == (_OWNER_REPO, "main"), (
        "the binding stores the canonical repo and the requested branch"
    )
    assert row.ma_secret_ref == "anon:", "a public bind mints no credential"
    assert row.proof_kind == "public", "the bind-time evidence is recorded as a public proof"
    assert row.proof_account_id == caller.id, "the proof is attributed to the person who asked"


async def test_bind_public_repo_refuses_a_private_repo_without_writing(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """An `anon:` binding is what the operator's fallback token clones, so a
    private repo must never reach one — not even for an admin."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client(tenant.id))
    auth = _auth_identity(tenant_id=tenant.id, account_id=caller.id, is_admin=True)
    github, _seen = _github(private=True)

    async with (
        github,
        turn_origin(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=caller.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
            responder_ma_agent_id=_RESPONDER_ID,
            responder_name="daimon",
            role=Role.USER,
        ) as origin,
    ):
        with pytest.raises(ToolError, match="request_repo_binding") as refusal:
            await _bind_public_repo_impl(
                runtime,
                auth,
                agent_name=_TARGET_NAME,
                repo_url=_REPO_URL,
                branch="main",
                origin_context_id=str(origin.id),
                expected_ma_agent_id=_TARGET_ID,
                unsaved_work=None,
                http_client=github,
            )

    assert "not a public GitHub repo" in str(refusal.value), (
        "the refusal names the actual blocker, not a generic failure"
    )
    assert await _binding(committing_sessionmaker, tenant_id=tenant.id) is None, (
        "a refused bind writes no binding row"
    )


async def test_bind_public_repo_refuses_a_shared_agent_for_a_non_admin_before_any_github_call(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client(tenant.id), default_agent_name=_TARGET_NAME)
    auth = _auth_identity(tenant_id=tenant.id, account_id=caller.id, is_admin=False)
    github, seen = _github()

    async with (
        github,
        turn_origin(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=caller.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
            responder_ma_agent_id=_RESPONDER_ID,
            responder_name="daimon",
            role=Role.USER,
        ) as origin,
    ):
        with pytest.raises(ToolError, match="needs a workspace or server admin") as refusal:
            await _bind_public_repo_impl(
                runtime,
                auth,
                agent_name=_TARGET_NAME,
                repo_url=_REPO_URL,
                branch="main",
                origin_context_id=str(origin.id),
                expected_ma_agent_id=_TARGET_ID,
                unsaved_work=None,
                http_client=github,
            )

    assert seen == [], (
        "the permission decision must precede the network call, so a refused caller "
        "cannot use this tool as an existence oracle for arbitrary repos"
    )
    assert f"give {_TARGET_NAME} access to {_OWNER_REPO}" in str(refusal.value), (
        "the refusal supplies the sentence an admin can say, with the target kept"
    )
    assert await _binding(committing_sessionmaker, tenant_id=tenant.id) is None, (
        "a refused bind writes no binding row"
    )


async def test_bind_public_repo_asks_about_uncommitted_work_once_then_stores_the_answer(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T_THREAD",
        account_id=caller.id,
        ma_session_id="sess_1",
        effective_config=_snapshot(repo_url="https://github.com/acme/old"),
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client(tenant.id))
    auth = _auth_identity(tenant_id=tenant.id, account_id=caller.id, is_admin=False)
    github, _seen = _github()

    async with (
        github,
        turn_origin(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=caller.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
            responder_ma_agent_id=_RESPONDER_ID,
            responder_name="daimon",
            role=Role.USER,
        ) as origin,
    ):
        with pytest.raises(ToolError, match="uncommitted changes in https://github.com/acme/old"):
            await _bind_public_repo_impl(
                runtime,
                auth,
                agent_name=_TARGET_NAME,
                repo_url=_REPO_URL,
                branch="main",
                origin_context_id=str(origin.id),
                expected_ma_agent_id=_TARGET_ID,
                unsaved_work=None,
                http_client=github,
            )
        assert await _binding(committing_sessionmaker, tenant_id=tenant.id) is None, (
            "asking the question must change nothing"
        )

        result = await _bind_public_repo_impl(
            runtime,
            auth,
            agent_name=_TARGET_NAME,
            repo_url=_REPO_URL,
            branch="main",
            origin_context_id=str(origin.id),
            expected_ma_agent_id=_TARGET_ID,
            unsaved_work="leave",
            http_client=github,
        )

    assert result.repo_url == _OWNER_REPO, "the answered retry goes through"
    async with committing_sessionmaker() as session:
        live = await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T_THREAD",
            account_id=caller.id,
        )
    assert live is not None and live.pending_unsaved_work == "leave", (
        "the checkout the answer governs is built at the caller's next message, so the "
        "answer has to be stored rather than lost with this turn"
    )
    assert await _binding(committing_sessionmaker, tenant_id=tenant.id) is not None, (
        "the answer and the binding land together"
    )


async def test_bind_public_repo_rejects_a_wrong_or_expired_origin_before_touching_github(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client(tenant.id))
    auth = _auth_identity(tenant_id=tenant.id, account_id=caller.id, is_admin=True)
    github, seen = _github()

    async with github:
        with pytest.raises(ToolError, match="unavailable or expired"):
            await _bind_public_repo_impl(
                runtime,
                auth,
                agent_name=_TARGET_NAME,
                repo_url=_REPO_URL,
                branch="main",
                origin_context_id=str(uuid.uuid4()),
                expected_ma_agent_id=_TARGET_ID,
                unsaved_work=None,
                http_client=github,
            )

    assert seen == [], "an untrusted caller never reaches the network"
    assert await _binding(committing_sessionmaker, tenant_id=tenant.id) is None, (
        "an untrusted caller never reaches the store either"
    )


async def test_bind_public_repo_confirmation_never_claims_a_copy(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The copy happens while the next checkout is built and is reported there
    with the count it actually copied. Claiming it at bind time would be
    claiming a result that has not happened yet."""
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T_THREAD",
        account_id=caller.id,
        ma_session_id="sess_1",
        effective_config=_snapshot(repo_url="https://github.com/acme/old"),
    )
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker, _client(tenant.id))
    auth = _auth_identity(tenant_id=tenant.id, account_id=caller.id, is_admin=False)
    github, _seen = _github()

    async with (
        github,
        turn_origin(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=caller.id,
            platform="discord",
            parent_channel_id="C_PARENT",
            thread_id="T_THREAD",
            responder_ma_agent_id=_RESPONDER_ID,
            responder_name="daimon",
            role=Role.USER,
        ) as origin,
    ):
        result = await _bind_public_repo_impl(
            runtime,
            auth,
            agent_name=_TARGET_NAME,
            repo_url=_REPO_URL,
            branch="main",
            origin_context_id=str(origin.id),
            expected_ma_agent_id=_TARGET_ID,
            unsaved_work="copy",
            http_client=github,
        )

    assert "copied" not in result.confirmation.lower(), (
        "no copy has happened yet, so the confirmation must not report one"
    )
    assert f"{_TARGET_NAME} now works in {_OWNER_REPO} on main." in result.confirmation, (
        "the confirmation still states the change that did happen"
    )
    async with committing_sessionmaker() as session:
        live = await get_live_thread_session(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T_THREAD",
            account_id=caller.id,
        )
    assert live is not None and live.pending_unsaved_work == "copy", (
        "the answer is still stored for the checkout that will act on it"
    )

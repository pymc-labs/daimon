"""Installation-token failures isolate repository groups without losing token inventory."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.core import github_app_session
from daimon.core._models import (
    AgentGitHubGrant,
    GitHubInstallationReconciliation,
    GitHubIssuedToken,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.config import GithubAppSettings
from daimon.core.credential_env import (
    github_repo_map_sha256,
    github_repo_script_sha256,
    render_github_repo_map,
)
from daimon.core.github_app_session import (
    AppSessionAccess,
    AppToken,
    GitHubTokenMintUnavailable,
    effective_repo_plan,
    effective_repo_url_sets,
    finish_app_delivery,
    prepare_app_access,
    revoke_app_access,
    rotate_live_app_tokens,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_requester_access import PermissionCache
from daimon.core.session_compat import (
    MaCapabilities,
    ReplaceSession,
    ReuseAsIs,
    RotateAppTokens,
    UpdateInPlace,
    decide_session_compatibility,
)
from daimon.core.session_preparation_stages import desired_snapshot_for
from daimon.core.session_snapshot import SessionSnapshot, desired_snapshot, token_scope_identity
from daimon.core.stores import github_access, github_app_installations, github_issued_tokens
from daimon.core.stores.github_issued_tokens import (
    select_stale_tokens,
    session_installations_needing_rotation,
    session_token_needs_rotation,
)
from daimon.core.stores.thread_sessions import create_thread_session, get_thread_session_by_id
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.prepare import (
    _delivered_app_snapshot_state,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.turn.termination import TerminationReason
from daimon.testing.ma_models import ma_agent
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _fake_jwt(*_args: Any, **_kwargs: Any) -> str:
    return "jwt"


async def _done_call(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _two_groups(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for repo_id, name in ((101, "example/read"), (102, "example/write")):
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=1,
                    installation_id=771,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=2,
                    status="active",
                    version=1,
                )
            )
        await session.flush()
        await github_app_installations.upsert_github_app(
            session,
            installation_id=771,
            account_id=1,
            account_login="example",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        for repo_id, access in ((101, "read"), (102, "write")):
            await github_access.stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=repo_id,
                baseline_access=cast(Literal["read", "write"], access),
                ceiling_access=cast(Literal["read", "write"], access),
                granted_by_account_id=None,
                is_working_repo=repo_id == 102,
            )
    return tenant_id, agent_id


async def _seed_multi_installation_grants(
    factory: async_sessionmaker[AsyncSession],
    *,
    repos: tuple[tuple[int, int, bool], ...],
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for installation_id, repo_id, _ in repos:
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=installation_id,
                    installation_id=installation_id,
                    repo_full_name=f"owner{installation_id}/repo{repo_id}",
                    max_access="write",
                    authorized_by_github_user_id=2,
                    status="active",
                    version=1,
                )
            )
        await session.flush()
        for installation_id in {installation_id for installation_id, _, _ in repos}:
            await github_app_installations.upsert_github_app(
                session,
                installation_id=installation_id,
                account_id=installation_id,
                account_login=f"owner{installation_id}",
                account_type="Organization",
                repository_selection="selected",
                suspended_at=None,
            )
        for _, repo_id, working in repos:
            session.add(
                AgentGitHubGrant(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_id=repo_id,
                    baseline_access="write",
                    ceiling_access="write",
                    mount_path=None,
                    is_working_repo=working,
                    granted_by_account_id=None,
                )
            )
    return tenant_id, agent_id


async def test_grant_added_after_create_delivery_does_not_change_snapshot_mode(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory, repos=((771, 101, True),)
    )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"token": "token-a"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        access = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="create-race",
            config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            cache=PermissionCache(),
        )
        await finish_app_delivery(
            db_session_factory,
            client,
            access=access,
            provisional_session_id="create-race",
            session_id="create-race-live",
        )
    assert access.tokens[0].chunk is None
    async with db_session_factory.begin() as session:
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=201,
                owner_id=772,
                installation_id=772,
                repo_full_name="owner772/repo201",
                max_access="write",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
        await session.flush()
        await github_app_installations.upsert_github_app(
            session,
            installation_id=772,
            account_id=772,
            account_login="owner772",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        session.add(
            AgentGitHubGrant(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=201,
                baseline_access="write",
                ceiling_access="write",
                mount_path=None,
                is_working_repo=False,
                granted_by_account_id=None,
            )
        )
    has_token, urls, scopes, issued_at = _delivered_app_snapshot_state(access)
    assert has_token
    assert urls == ("https://github.com/owner771/repo101",)
    assert scopes == ()
    assert issued_at == {}


async def test_concurrent_grant_removal_uses_frozen_multi_mode_and_rolls_back(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory, repos=((771, 101, True), (772, 201, False))
    )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)
    minted: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        scope = json.loads(request.content)["repository_ids"]
        minted.append(scope)
        return httpx.Response(201, json={"token": f"token-{scope[0]}-{len(minted)}"})

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        initial = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="race-pending",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        await finish_app_delivery(
            db_session_factory,
            client,
            access=initial,
            provisional_session_id="race-pending",
            session_id="race-live",
        )
    minted.clear()
    real_prepare = prepare_app_access

    async def remove_a_then_prepare(*args: Any, **kwargs: Any):
        assert kwargs["grant_view"][0]  # type: ignore[index]
        async with db_session_factory.begin() as session:
            grant = await session.scalar(
                select(AgentGitHubGrant).where(
                    AgentGitHubGrant.tenant_id == tenant_id,
                    AgentGitHubGrant.agent_id == agent_id,
                    AgentGitHubGrant.repo_id == 101,
                )
            )
            assert grant is not None
            await session.delete(grant)
        kwargs.pop("anthropic", None)
        kwargs.pop("vault_id", None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await real_prepare(db_session_factory, client, **kwargs)

    monkeypatch.setattr(github_app_session, "prepare_app_access", remove_a_then_prepare)
    with pytest.raises(ValueError):
        await rotate_live_app_tokens(
            None,  # type: ignore[arg-type]  # vault update must never be reached
            db_session_factory,
            session_id="race-live",
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            vault_id="vault",
            resource_ids={},
            config=config,
            fernet=fernet,
            installation_ids=(771,),
        )
    assert minted == [[101]]
    async with db_session_factory() as session:
        live = await github_issued_tokens.list_session_tokens(session, session_id="race-live")
    assert {row.installation_id for row in live if row.status == "delivered"} == {771, 772}


def test_combine_refuses_single_vault_with_retained_multi_label() -> None:
    token = AppToken(uuid.uuid4(), "old-b", 772, (201,), "write", "GH_TOKEN_I772_0", 10, 0)
    previous = AppSessionAccess(
        (),
        (token,),
        None,
        token_scopes=((772, 0, (201,), 10, str(token.token_id)),),
        multi_installation=True,
    )
    replacement = AppSessionAccess((), (), None, multi_installation=False)
    with pytest.raises(ValueError, match="mode changed"):
        github_app_session._combine_app_access(  # pyright: ignore[reportPrivateUsage]
            replacement, previous, frozenset({771})
        )


@pytest.mark.asyncio
async def test_revoked_installation_with_live_grants_forces_rotation(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory, repos=((771, 101, True), (772, 201, False))
    )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        scope = json.loads(request.content)["repository_ids"]
        return httpx.Response(201, json={"token": f"token-{scope[0]}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        access = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="pending-revoked",
            config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            cache=PermissionCache(),
        )
        await finish_app_delivery(
            db_session_factory,
            client,
            access=access,
            provisional_session_id="pending-revoked",
            session_id="live-revoked",
        )
    async with db_session_factory.begin() as session:
        a_row = await session.scalar(
            select(GitHubIssuedToken).where(
                GitHubIssuedToken.session_id == "live-revoked",
                GitHubIssuedToken.installation_id == 771,
            )
        )
        assert a_row is not None
        a_row.status = "revoked"
        a_row.revoked_at = datetime.now(UTC)
    async with db_session_factory() as session:
        assert await session_installations_needing_rotation(session, session_id="live-revoked") == (
            771,
        )
        assert await session_token_needs_rotation(session, session_id="live-revoked")


async def test_revoked_chunk_forces_whole_installation_refresh_without_renumbering(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory,
        repos=((771, 101, True), (771, 102, False), (772, 201, False)),
    )

    def groups(*args: Any, **kwargs: Any):
        return [
            (771, "write", (101,)),
            (771, "write", (102,)),
            (772, "write", (201,)),
        ]

    monkeypatch.setattr(github_app_session, "group_repository_access", groups)
    monkeypatch.setattr(github_issued_tokens, "group_repository_access", groups)
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)
    mint_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mint_count
        if request.method == "DELETE":
            return httpx.Response(204)
        mint_count += 1
        scope = json.loads(request.content)["repository_ids"]
        return httpx.Response(201, json={"token": f"token-{scope[0]}-{mint_count}"})

    real_prepare = prepare_app_access

    async def prepare_with_transport(*args: Any, **kwargs: Any):
        kwargs.pop("anthropic", None)
        kwargs.pop("vault_id", None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await real_prepare(db_session_factory, client, **kwargs)

    monkeypatch.setattr(github_app_session, "prepare_app_access", prepare_with_transport)
    monkeypatch.setattr(github_app_session, "add_app_credentials", _done_call)
    monkeypatch.setattr(github_app_session, "rotate_session_repo_token", _done_call)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        initial = await real_prepare(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="pending-one-chunk-revoked",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        assert {
            (scope[0], scope[1], scope[3], scope[4])
            for scope in initial.token_scopes
            if len(scope) == 5
        } == {
            (token.installation_id, token.chunk, token.issued_at, str(token.token_id))
            for token in initial.tokens
        }
        await finish_app_delivery(
            db_session_factory,
            client,
            access=initial,
            provisional_session_id="pending-one-chunk-revoked",
            session_id="one-chunk-revoked",
        )
    async with db_session_factory() as session:
        assert (
            await session_installations_needing_rotation(session, session_id="one-chunk-revoked")
            == ()
        )
    async with db_session_factory.begin() as session:
        await github_issued_tokens.mark_revoked(session, token_id=initial.tokens[0].token_id)
    async with db_session_factory() as session:
        assert await session_installations_needing_rotation(
            session, session_id="one-chunk-revoked"
        ) == (771,)

    rotated_a = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # MA calls are stubbed above
        db_session_factory,
        session_id="one-chunk-revoked",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={},
        config=config,
        fernet=fernet,
        installation_ids=(771,),
    )
    assert {token.credential_name for token in rotated_a.tokens} == {
        "GH_TOKEN_I771_0",
        "GH_TOKEN_I771_1",
        "GH_TOKEN_I772_0",
    }
    rotated_b = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # MA calls are stubbed above
        db_session_factory,
        session_id="one-chunk-revoked",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={},
        config=config,
        fernet=fernet,
        installation_ids=(772,),
    )
    rebuilt = await github_app_session._current_app_access(  # pyright: ignore[reportPrivateUsage]
        db_session_factory,
        session_id="one-chunk-revoked",
        tenant_id=tenant_id,
        agent_id=agent_id,
        fernet=fernet,
    )
    assert {token.credential_name: token.token for token in rebuilt.tokens} == {
        token.credential_name: token.token for token in rotated_b.tokens
    }
    async with db_session_factory() as session:
        rows = await github_issued_tokens.list_session_tokens(
            session, session_id="one-chunk-revoked"
        )
    live_rows = [row for row in rows if row.superseded_at is None]
    assert {token.token_id for token in rebuilt.tokens} == {row.token_id for row in live_rows}
    assert all(row.chunk is not None for row in live_rows)
    assert {token.credential_name for token in rebuilt.tokens} == {
        f"GH_TOKEN_I{row.installation_id}_{row.chunk}" for row in live_rows
    }


async def test_failed_working_installation_never_aliases_surviving_token(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory, repos=((771, 101, True), (772, 201, False))
    )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        scope = json.loads(request.content)["repository_ids"]
        if scope == [101]:
            return httpx.Response(422)
        return httpx.Response(201, json={"token": "b-token"})

    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        access = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="working-failed",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
    assert access.multi_installation
    assert [token.installation_id for token in access.tokens] == [772]
    assert access.working_repo_id == 101
    assert access.working_token is None
    assert access.resources == ()
    assert token_scope_identity(access.token_scopes) == ((772, (201,)),)
    stored_names: list[str] = []

    async def empty_credentials(*args: Any, **kwargs: Any):
        for credential in ():
            yield credential

    async def store_credential(*args: Any, **kwargs: Any) -> None:
        payload = args[2]
        assert isinstance(payload, dict)
        stored_names.append(cast(str, payload["auth"]["secret_name"]))

    async def unexpected_copilot(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("failed working token must not create Copilot access")

    monkeypatch.setattr(github_app_session, "list_credentials", empty_credentials)
    monkeypatch.setattr(github_app_session, "store_credential", store_credential)
    monkeypatch.setattr(github_app_session, "add_github_copilot_credential", unexpected_copilot)
    await github_app_session.add_app_credentials(
        cast(AsyncAnthropic, object()), vault_id="vault", access=access
    )
    assert stored_names == ["GH_TOKEN_I772_0"]
    assert access.repo_credential_map == (("owner772/repo201", "GH_TOKEN_I772_0"),)

    agent = ma_agent(id="agent_working_refused")
    now = datetime.now(UTC)
    recorded = desired_snapshot(
        agent,
        hidden_mcp_server_names=frozenset(),
        environment_id="environment",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id="vault",
        token_repo_urls=access.token_repo_urls,
        token_scopes=access.token_scopes,
        github_repo_map_sha256=github_repo_map_sha256(access.repo_credential_map),
        github_repo_script_sha256=github_repo_script_sha256(),
    ).model_copy(
        update={"unavailable_repos": tuple((url, now) for url in access.excluded_repo_urls)}
    )
    desired = await desired_snapshot_for(
        db_session_factory,
        agent=agent,
        environment_id="environment",
        tenant_id=tenant_id,
        agent_uuid=agent_id,
        account_id=uuid.uuid4(),
        github_app=config,
        fernet=fernet,
        recorded=recorded,
        tool_safety=OPEN_TOOL_SAFETY,
        public_url=None,
    )
    assert desired.github_repo_map_sha256 == recorded.github_repo_map_sha256
    assert desired.github_repo_script_sha256 == recorded.github_repo_script_sha256
    assert token_scope_identity(desired.token_scopes) == token_scope_identity(access.token_scopes)
    assert (
        decide_session_compatibility(
            recorded=recorded, desired=desired, capabilities=MaCapabilities(), now=now
        )
        == ReuseAsIs()
    )


@pytest.mark.parametrize("failed_repo_id", [101, 102])
async def test_failed_installation_chunk_is_atomic_and_inventory_names_survive_b_rotation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failed_repo_id: int,
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory,
        repos=((771, 101, True), (771, 102, False), (772, 201, False)),
    )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)

    # Three tiny groups expose the same chunk failure boundary as >500 repos.
    def groups(_grants: Any, **_kwargs: Any) -> list[tuple[int, str, tuple[int, ...]]]:
        return [
            (771, "write", (101,)),
            (771, "write", (102,)),
            (772, "write", (201,)),
        ]

    monkeypatch.setattr(
        github_app_session,
        "group_repository_access",
        groups,
    )
    minted_scopes: list[list[int]] = []
    revoked_values: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            revoked_values.append(request.headers["Authorization"].removeprefix("Bearer "))
            return httpx.Response(204)
        scope = json.loads(request.content)["repository_ids"]
        minted_scopes.append(scope)
        if scope == [failed_repo_id]:
            return httpx.Response(422)
        return httpx.Response(201, json={"token": f"token-{scope[0]}-{len(minted_scopes)}"})

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    real_prepare = prepare_app_access

    async def prepare_with_transport(*args: Any, **kwargs: Any):
        kwargs.pop("anthropic", None)
        kwargs.pop("vault_id", None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await real_prepare(db_session_factory, client, **kwargs)

    monkeypatch.setattr(github_app_session, "prepare_app_access", prepare_with_transport)
    monkeypatch.setattr(github_app_session, "add_app_credentials", _done_call)
    monkeypatch.setattr(github_app_session, "rotate_session_repo_token", _done_call)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        initial = await real_prepare(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="atomic-pending",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        assert [token.installation_id for token in initial.tokens] == [772]
        assert initial.excluded_repo_urls == (
            "https://github.com/owner771/repo101",
            "https://github.com/owner771/repo102",
        )
        assert initial.working_token is None
        assert initial.repo_credential_map == (("owner772/repo201", "GH_TOKEN_I772_0"),)
        await finish_app_delivery(
            db_session_factory,
            client,
            access=initial,
            provisional_session_id="atomic-pending",
            session_id="atomic-live",
        )
    assert [scope for scope in minted_scopes if scope[0] in (101, 102)] == (
        [[101]] if failed_repo_id == 101 else [[101], [102]]
    )
    if failed_repo_id == 102:
        assert any(value.startswith("token-101-") for value in revoked_values)

    rotated = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # MA calls are stubbed above
        db_session_factory,
        session_id="atomic-live",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={},
        config=config,
        fernet=fernet,
        installation_ids=(772,),
    )
    assert rotated.working_token is None
    assert rotated.repo_credential_map == (("owner772/repo201", "GH_TOKEN_I772_0"),)
    assert {token.credential_name: token.token for token in rotated.tokens} == {
        "GH_TOKEN_I772_0": rotated.tokens[0].token
    }
    rebuilt = await github_app_session._current_app_access(  # pyright: ignore[reportPrivateUsage]
        db_session_factory,
        session_id="atomic-live",
        tenant_id=tenant_id,
        agent_id=agent_id,
        fernet=fernet,
    )
    assert {token.credential_name: token.token for token in rebuilt.tokens} == {
        token.credential_name: token.token for token in rotated.tokens
    }
    assert rebuilt.working_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("has_working_repo", [True, False])
async def test_new_repo_in_b_remints_only_b_and_preserves_a(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    has_working_repo: bool,
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with db_session_factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for installation_id, repo_id, name in (
            (771, 101, "a/one"),
            (772, 102, "b/two"),
            (772, 103, "b/three"),
        ):
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=installation_id,
                    installation_id=installation_id,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=2,
                    status="active",
                    version=1,
                )
            )
        await session.flush()
        for installation_id, owner in ((771, "a"), (772, "b")):
            await github_app_installations.upsert_github_app(
                session,
                installation_id=installation_id,
                account_id=installation_id,
                account_login=owner,
                account_type="Organization",
                repository_selection="selected",
                suspended_at=None,
            )
        # M1 keeps stage_grant's second-installation guard. Seed B directly.
        for repo_id in (101, 102):
            session.add(
                AgentGitHubGrant(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_id=repo_id,
                    baseline_access="write",
                    ceiling_access="write",
                    mount_path=None,
                    is_working_repo=repo_id == 101 and has_working_repo,
                    granted_by_account_id=None,
                )
            )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)
    mint_scopes: list[list[int]] = []
    fail_b_read = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        scope = json.loads(request.content)["repository_ids"]
        mint_scopes.append(scope)
        if fail_b_read and scope == [102]:
            return httpx.Response(422)
        return httpx.Response(201, json={"token": f"token-{len(mint_scopes)}"})

    real_prepare = prepare_app_access

    async def prepare_with_transport(*args: Any, **kwargs: Any):
        kwargs.pop("anthropic", None)
        kwargs.pop("vault_id", None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await real_prepare(db_session_factory, client, **kwargs)

    monkeypatch.setattr(github_app_session, "prepare_app_access", prepare_with_transport)
    monkeypatch.setattr(github_app_session, "add_app_credentials", _done_call)
    monkeypatch.setattr(github_app_session, "rotate_session_repo_token", _done_call)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        initial = await real_prepare(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="pending-initial",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        assert mint_scopes == [[101], [102]]
        assert [token.credential_name for token in initial.tokens] == [
            "GH_TOKEN_I771_0",
            "GH_TOKEN_I772_0",
        ]
        assert initial.repo_credential_map == (
            ("a/one", "GH_TOKEN_I771_0"),
            ("b/two", "GH_TOKEN_I772_0"),
        )
        assert token_scope_identity(initial.token_scopes) == ((771, (101,)), (772, (102,)))
        assert (initial.working_token is not None) == has_working_repo
        await finish_app_delivery(
            db_session_factory,
            client,
            access=initial,
            provisional_session_id="pending-initial",
            session_id="live-session",
        )
    async with db_session_factory.begin() as session:
        mapped = await create_thread_session(
            session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id="two-installations",
            account_id=uuid.uuid4(),
            ma_session_id="live-session",
            effective_config=SessionSnapshot.model_validate(
                {
                    "ma_agent_id": "agent_rotation",
                    "model_id": "test-model",
                    "system_sha256": None,
                    "skills_sha256": "skills",
                    "environment_id": "environment",
                    "github_mode": "app",
                    "repo_urls": ["https://github.com/a/one"] if has_working_repo else [],
                    "token_repo_urls": ["https://github.com/a/one", "https://github.com/b/two"],
                    "token_scopes": initial.token_scopes,
                    "token_issued_at_by_installation": {
                        token.installation_id: token.issued_at for token in initial.tokens
                    },
                    "repo_url": None,
                    "repo_branch": None,
                    "memory_store_id": None,
                    "vault_id": "vault",
                    "tools_sha256": "tools",
                    "mcp_servers_sha256": "servers",
                    "env_sha256": None,
                    "agent_version": 1,
                    "agent_name": "Agent",
                }
            ),
        )
    async with db_session_factory.begin() as session:
        session.add(
            AgentGitHubGrant(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=103,
                baseline_access="write",
                ceiling_access="write",
                mount_path=None,
                is_working_repo=False,
                granted_by_account_id=None,
            )
        )
    mint_scopes.clear()
    rotated = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # MA calls are stubbed above
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={},
        config=config,
        fernet=fernet,
        installation_ids=(772,),
    )
    assert mint_scopes == [[102, 103]]
    assert rotated.tokens[0].token_id == initial.tokens[0].token_id
    assert rotated.tokens[0].token == initial.tokens[0].token
    assert token_scope_identity(rotated.token_scopes) == ((771, (101,)), (772, (102, 103)))
    assert rotated.repo_credential_map == (
        ("a/one", "GH_TOKEN_I771_0"),
        ("b/three", "GH_TOKEN_I772_0"),
        ("b/two", "GH_TOKEN_I772_0"),
    )
    assert (rotated.working_token is not None) == has_working_repo
    async with db_session_factory() as session:
        rows = list(
            await session.scalars(
                select(GitHubIssuedToken).where(GitHubIssuedToken.agent_id == agent_id)
            )
        )
        mapping = await get_thread_session_by_id(session, id=mapped.id)
    assert mapping is not None and mapping.effective_config is not None
    assert mapping.effective_config.token_scopes == rotated.token_scopes
    assert mapping.effective_config.token_issued_at_by_installation[771] == (
        initial.tokens[0].issued_at
    )
    assert mapping.effective_config.token_issued_at_by_installation[772] >= (
        initial.tokens[1].issued_at or 0
    )
    a_row = next(row for row in rows if row.installation_id == 771)
    assert a_row.status == "delivered" and a_row.superseded_at is None
    async with db_session_factory.begin() as session:
        await github_access.remove_grant(
            session, tenant_id=tenant_id, agent_id=agent_id, repo_id=101
        )
    if not has_working_repo:
        # The sweeper can revoke A before the next turn reads inventory. The
        # persisted A/B scope must still make this an A-only contraction.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await revoke_app_access(
                db_session_factory, client, replace(rotated, tokens=(rotated.tokens[0],))
            )
    async with db_session_factory() as session:
        stale = await select_stale_tokens(session)
        due = await session_installations_needing_rotation(session, session_id="live-session")
    assert {row.token_id for row in stale if row.agent_id == agent_id} == (
        set() if not has_working_repo else {a_row.token_id}
    )
    assert due == (771,)
    if not has_working_repo:
        desired = mapping.effective_config.model_copy(
            update={
                "token_repo_urls": (
                    "https://github.com/b/two",
                    "https://github.com/b/three",
                ),
                "token_scopes": (),
            }
        )
        decision = decide_session_compatibility(
            recorded=mapping.effective_config,
            desired=desired,
            capabilities=MaCapabilities(),
            now=datetime.now(UTC),
            forced_app_installation_ids=due,
        )
        assert isinstance(decision, UpdateInPlace)
        assert RotateAppTokens(resource_ids={}, installation_ids=()) in decision.ops
    mint_scopes.clear()
    contracted = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # MA calls are stubbed above
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={},
        config=config,
        fernet=fernet,
        installation_ids=(771,),
    )
    assert mint_scopes == [[102, 103]]
    assert [token.token_id for token in contracted.tokens] != [rotated.tokens[1].token_id]
    assert contracted.tokens[0].chunk is None
    assert contracted.token_scopes == ()
    async with db_session_factory() as session:
        contracted_mapping = await get_thread_session_by_id(session, id=mapped.id)
        assert (
            await session_installations_needing_rotation(session, session_id="live-session") == ()
        )
    assert contracted_mapping is not None and contracted_mapping.effective_config is not None
    assert contracted_mapping.effective_config.token_scopes == ()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await revoke_app_access(
            db_session_factory, client, replace(rotated, tokens=(rotated.tokens[0],))
        )
    async with db_session_factory() as session:
        surviving = list(
            await session.scalars(
                select(GitHubIssuedToken).where(GitHubIssuedToken.agent_id == agent_id)
            )
        )
    assert next(row for row in surviving if row.installation_id == 771).status == "revoked"
    assert any(row.installation_id == 772 and row.status == "delivered" for row in surviving)
    if not has_working_repo:
        async with db_session_factory.begin() as session:
            read_grant = await session.scalar(
                select(AgentGitHubGrant).where(
                    AgentGitHubGrant.tenant_id == tenant_id,
                    AgentGitHubGrant.agent_id == agent_id,
                    AgentGitHubGrant.repo_id == 102,
                )
            )
            assert read_grant is not None
            read_grant.baseline_access = "read"
            read_grant.ceiling_access = "read"
            read_grant.version += 1
        fail_b_read = True
        mint_scopes.clear()
        after_failed_read = await rotate_live_app_tokens(
            None,  # type: ignore[arg-type]  # MA calls are stubbed above
            db_session_factory,
            session_id="live-session",
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            vault_id="vault",
            resource_ids={},
            config=config,
            fernet=fernet,
        )
        assert mint_scopes == [[102], [103]]
        assert [token.repo_ids for token in after_failed_read.tokens] == [(103,)]
        assert after_failed_read.token_scopes == ()
        assert after_failed_read.tokens[0].chunk is None
        async with db_session_factory() as session:
            assert not await session_token_needs_rotation(session, session_id="live-session")
            final_mapping = await get_thread_session_by_id(session, id=mapped.id)
        assert final_mapping is not None and final_mapping.effective_config is not None
        assert final_mapping.effective_config.token_scopes == ()
        issued_at = final_mapping.effective_config.repo_token_issued_at
        assert issued_at is not None
        assert (
            decide_session_compatibility(
                recorded=final_mapping.effective_config,
                desired=final_mapping.effective_config,
                capabilities=MaCapabilities(),
                now=datetime.fromtimestamp(issued_at, UTC) + timedelta(minutes=39),
            )
            == ReuseAsIs()
        )


@pytest.mark.asyncio
async def test_contraction_of_501_repo_installation_enters_legacy_mode_once(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory,
        repos=((771, 900, False), *((772, repo_id, False) for repo_id in (501, *range(1, 501)))),
    )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)
    original_list = github_access.list_live_grant_repositories

    async def ordered_grants(session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID):
        rows = await original_list(session, tenant_id=tenant_id, agent_id=agent_id)
        return sorted(
            rows,
            key=lambda item: (
                item[1].installation_id != 771,
                item[1].repo_id != 501,
                item[1].repo_id,
            ),
        )

    monkeypatch.setattr(github_app_session, "list_live_grant_repositories", ordered_grants)
    minted: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        scope = json.loads(request.content)["repository_ids"]
        minted.append(scope)
        return httpx.Response(201, json={"token": f"token-{len(minted)}"})

    real_prepare = prepare_app_access

    async def prepare_with_transport(*args: Any, **kwargs: Any):
        kwargs.pop("anthropic", None)
        kwargs.pop("vault_id", None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await real_prepare(db_session_factory, client, **kwargs)

    monkeypatch.setattr(github_app_session, "prepare_app_access", prepare_with_transport)
    monkeypatch.setattr(github_app_session, "add_app_credentials", _done_call)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        initial = await real_prepare(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="pending-501",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        assert minted == [[900], list(range(1, 501)), [501]]
        mapped_repos = dict(initial.repo_credential_map)
        assert len(mapped_repos) == 502
        assert mapped_repos["owner771/repo900"] == "GH_TOKEN_I771_0"
        assert mapped_repos["owner772/repo1"] == "GH_TOKEN_I772_0"
        assert mapped_repos["owner772/repo500"] == "GH_TOKEN_I772_0"
        assert mapped_repos["owner772/repo501"] == "GH_TOKEN_I772_1"
        await finish_app_delivery(
            db_session_factory,
            client,
            access=initial,
            provisional_session_id="pending-501",
            session_id="live-501",
        )
    async with db_session_factory.begin() as session:
        mapped = await create_thread_session(
            session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id="contraction-501",
            account_id=uuid.uuid4(),
            ma_session_id="live-501",
            effective_config=SessionSnapshot.model_validate(
                {
                    "ma_agent_id": "agent_501",
                    "model_id": "test-model",
                    "system_sha256": None,
                    "skills_sha256": "skills",
                    "environment_id": "environment",
                    "github_mode": "app",
                    "repo_urls": [],
                    "token_repo_urls": initial.token_repo_urls,
                    "token_scopes": initial.token_scopes,
                    "repo_url": None,
                    "repo_branch": None,
                    "memory_store_id": None,
                    "vault_id": "vault",
                    "tools_sha256": "tools",
                    "mcp_servers_sha256": "servers",
                    "env_sha256": None,
                    "agent_version": 1,
                    "agent_name": "Agent",
                }
            ),
        )
        await github_access.remove_grant(
            session, tenant_id=tenant_id, agent_id=agent_id, repo_id=900
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await revoke_app_access(
            db_session_factory, client, replace(initial, tokens=(initial.tokens[0],))
        )
    async with db_session_factory() as session:
        assert await session_installations_needing_rotation(session, session_id="live-501") == (
            771,
        )
    minted.clear()
    contracted = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # MA vault call is stubbed above
        db_session_factory,
        session_id="live-501",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={},
        config=config,
        fernet=fernet,
        installation_ids=(771,),
    )
    assert minted == [[500], [501, *range(1, 500)]]
    assert [token.repo_ids for token in contracted.tokens] == [
        (500,),
        (501, *range(1, 500)),
    ]
    assert contracted.token_scopes == ()
    assert all(token.chunk is None for token in contracted.tokens)
    assert contracted.working_token == contracted.tokens[0].token
    async with db_session_factory() as session:
        rows = await github_issued_tokens.list_session_tokens(session, session_id="live-501")
        current = await get_thread_session_by_id(session, id=mapped.id)
        assert await session_installations_needing_rotation(session, session_id="live-501") == ()
    assert current is not None and current.effective_config is not None
    assert all(row.chunk is None for row in rows if row.superseded_at is None)
    assert current.effective_config.token_scopes == ()
    desired_urls, desired_mounts, desired_scopes = await effective_repo_url_sets(
        db_session_factory,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        config=config,
        fernet=fernet,
    )
    assert desired_scopes == ()
    desired = current.effective_config.model_copy(
        update={
            "token_repo_urls": desired_urls,
            "repo_urls": desired_mounts,
            "token_scopes": desired_scopes,
        }
    )
    issued_at = current.effective_config.repo_token_issued_at
    assert issued_at is not None
    assert (
        decide_session_compatibility(
            recorded=current.effective_config,
            desired=desired,
            capabilities=MaCapabilities(),
            now=datetime.fromtimestamp(issued_at, UTC) + timedelta(minutes=39),
        )
        == ReuseAsIs()
    )
    deadline = decide_session_compatibility(
        recorded=current.effective_config,
        desired=desired,
        capabilities=MaCapabilities(),
        now=datetime.fromtimestamp(issued_at, UTC) + timedelta(minutes=40),
    )
    assert isinstance(deadline, UpdateInPlace)
    assert RotateAppTokens(resource_ids={}, installation_ids=()) in deadline.ops

    # Adding A again crosses the mode boundary in the other direction. A
    # partial caller request must still re-mint B into sorted, labelled chunks.
    async with db_session_factory.begin() as session:
        session.add(
            AgentGitHubGrant(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=900,
                baseline_access="write",
                ceiling_access="write",
                mount_path=None,
                is_working_repo=False,
                granted_by_account_id=None,
            )
        )
    minted.clear()
    expanded = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # MA vault call is stubbed above
        db_session_factory,
        session_id="live-501",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={},
        config=config,
        fernet=fernet,
        installation_ids=(771,),
    )
    assert minted == [[900], list(range(1, 501)), [501]]
    assert expanded.multi_installation
    assert [token.chunk for token in expanded.tokens] == [0, 0, 1]
    assert len(expanded.token_scopes) == 3
    assert dict(expanded.repo_credential_map)["owner772/repo501"] == "GH_TOKEN_I772_1"


@pytest.mark.asyncio
async def test_vault_budget_refuses_before_token_mint(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _two_groups(db_session_factory)
    calls: list[str] = []

    async def existing_credentials(*args: Any, **kwargs: Any):
        for number in range(14):
            yield SimpleNamespace(id=f"other-{number}", auth=SimpleNamespace(type="static_bearer"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(201, json={"token": "unexpected"})

    monkeypatch.setattr(github_app_session, "list_credentials", existing_credentials)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="15-credential"):
            await prepare_app_access(
                db_session_factory,
                client,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=None,
                provisional_session_id="pending-budget",
                config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
                fernet=build_multifernet((Fernet.generate_key().decode(),)),
                cache=PermissionCache(),
                anthropic=cast(AsyncAnthropic, object()),
                vault_id="vault",
            )
    assert calls == []


async def test_twelve_installations_without_alias_fit_three_other_vault_credentials(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory,
        repos=tuple((installation_id, installation_id, False) for installation_id in range(1, 13)),
    )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)

    async def existing_credentials(*args: Any, **kwargs: Any):
        for number in range(3):
            yield SimpleNamespace(id=f"other-{number}", auth=SimpleNamespace(type="static_bearer"))

    created_names: list[str] = []

    async def store_credential(*args: Any, **kwargs: Any) -> None:
        payload = args[2]
        assert isinstance(payload, dict)
        created_names.append(cast(str, payload["auth"]["secret_name"]))

    async def unexpected_copilot(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no working repo means no Copilot credential")

    def handler(request: httpx.Request) -> httpx.Response:
        scope = json.loads(request.content)["repository_ids"]
        return httpx.Response(201, json={"token": f"token-{scope[0]}"})

    monkeypatch.setattr(github_app_session, "list_credentials", existing_credentials)
    monkeypatch.setattr(github_app_session, "store_credential", store_credential)
    monkeypatch.setattr(github_app_session, "add_github_copilot_credential", unexpected_copilot)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        access = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="twelve-pending",
            config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            cache=PermissionCache(),
            anthropic=cast(AsyncAnthropic, object()),
            vault_id="vault",
        )
    assert len(access.tokens) == 12
    assert access.working_token is None
    await github_app_session.add_app_credentials(
        cast(AsyncAnthropic, object()), vault_id="vault", access=access
    )
    assert created_names == [f"GH_TOKEN_I{installation_id}_0" for installation_id in range(1, 13)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 422])
async def test_coverage_mint_failure_keeps_other_group_and_dedupes_reconciliation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    tenant_id, agent_id = await _two_groups(db_session_factory)
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", _fake_jwt)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        ids = json.loads(request.content)["repository_ids"]
        return (
            httpx.Response(status)
            if ids == [102]
            else httpx.Response(201, json={"token": "read-token"})
        )

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for attempt in range(2):
            access = await prepare_app_access(
                db_session_factory,
                client,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=None,
                provisional_session_id=f"pending-{attempt}",
                config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
                fernet=fernet,
                cache=PermissionCache(),
            )
            assert [token.repo_ids for token in access.tokens] == [(101,)]
            assert access.excluded_repo_urls == ("https://github.com/example/write",)
            assert access.resources == ()
            async with db_session_factory() as session:
                rows = list(
                    await session.scalars(
                        select(GitHubIssuedToken).where(
                            GitHubIssuedToken.session_id == f"pending-{attempt}"
                        )
                    )
                )
                assert sorted(row.status for row in rows) == ["revoked", "stored"]
                job = await session.get(GitHubInstallationReconciliation, 771)
                assert job is not None and job.generation == 1


@pytest.mark.asyncio
async def test_single_installation_failed_read_group_keeps_legacy_refresh_deadline(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _two_groups(db_session_factory)
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        ids = json.loads(request.content)["repository_ids"]
        return (
            httpx.Response(422)
            if ids == [101]
            else httpx.Response(201, json={"token": "write-token"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        access = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="single-read-failed",
            config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            cache=PermissionCache(),
        )
        await finish_app_delivery(
            db_session_factory,
            client,
            access=access,
            provisional_session_id="single-read-failed",
            session_id="single-live",
        )
    assert access.token_scopes == ()
    assert access.working_token == "write-token"
    assert access.tokens[0].chunk is None
    async with db_session_factory() as session:
        rows = await github_issued_tokens.list_session_tokens(session, session_id="single-live")
        assert all(row.chunk is None for row in rows)
        assert await session_installations_needing_rotation(session, session_id="single-live") == ()
        assert not await session_token_needs_rotation(session, session_id="single-live")

    issued_at = access.tokens[0].issued_at
    assert issued_at is not None
    snapshot = SessionSnapshot.model_validate(
        {
            "ma_agent_id": "agent_single",
            "model_id": "test-model",
            "system_sha256": None,
            "skills_sha256": "skills",
            "environment_id": "environment",
            "github_mode": "app",
            "repo_urls": ["https://github.com/example/write"],
            "token_repo_urls": ["https://github.com/example/write"],
            "repo_token_issued_at": issued_at,
            "repo_url": None,
            "repo_branch": None,
            "memory_store_id": None,
            "vault_id": "vault",
            "tools_sha256": "tools",
            "mcp_servers_sha256": "servers",
            "env_sha256": None,
            "agent_version": 1,
            "agent_name": "Agent",
        }
    )
    assert (
        decide_session_compatibility(
            recorded=snapshot,
            desired=snapshot,
            capabilities=MaCapabilities(),
            now=datetime.fromtimestamp(issued_at, UTC) + timedelta(minutes=39),
        )
        == ReuseAsIs()
    )
    due = decide_session_compatibility(
        recorded=snapshot,
        desired=snapshot,
        capabilities=MaCapabilities(),
        now=datetime.fromtimestamp(issued_at, UTC) + timedelta(minutes=40),
    )
    assert isinstance(due, UpdateInPlace)
    assert RotateAppTokens(resource_ids={}) in due.ops


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 500, "timeout"])
async def test_transient_mint_failure_closes_rows_and_revokes_issued_token(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failure: int | str,
) -> None:
    tenant_id, agent_id = await _two_groups(db_session_factory)
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", _fake_jwt)
    revoked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            revoked.append(request.headers["Authorization"])
            return httpx.Response(204)
        if json.loads(request.content)["repository_ids"] == [101]:
            return httpx.Response(201, json={"token": "read-token"})
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        assert isinstance(failure, int)
        return httpx.Response(failure)

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubTokenMintUnavailable) as caught:
            await prepare_app_access(
                db_session_factory,
                client,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=None,
                provisional_session_id="pending-failed",
                config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
                fernet=fernet,
                cache=PermissionCache(),
            )
    notice = render_termination_notice(TerminationReason.UPSTREAM, error=caught.value)
    assert notice is not None and notice.title == "GitHub access is temporarily unavailable."
    assert "again" in notice.next_step
    assert revoked == ["Bearer read-token"]
    async with db_session_factory() as session:
        rows = list(
            await session.scalars(
                select(GitHubIssuedToken).where(GitHubIssuedToken.session_id == "pending-failed")
            )
        )
        assert len(rows) == 2 and all(row.status == "revoked" for row in rows)
        assert await session.get(GitHubInstallationReconciliation, 771) is None


@pytest.mark.asyncio
async def test_rotation_keeps_successful_group_when_other_group_returns_404(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _two_groups(db_session_factory)
    read_url = "https://github.com/example/read"
    write_url = "https://github.com/example/write"
    async with db_session_factory.begin() as session:
        mapped = await create_thread_session(
            session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id="rotation-refusal",
            account_id=uuid.uuid4(),
            ma_session_id="live-session",
            effective_config=SessionSnapshot.model_validate(
                {
                    "ma_agent_id": "agent_rotation",
                    "model_id": "test-model",
                    "system_sha256": None,
                    "skills_sha256": "skills",
                    "environment_id": "environment",
                    "github_mode": "app",
                    "repo_urls": [write_url],
                    "token_repo_urls": [read_url, write_url],
                    "repo_url": None,
                    "repo_branch": None,
                    "memory_store_id": None,
                    "vault_id": "vault",
                    "tools_sha256": "tools",
                    "mcp_servers_sha256": "servers",
                    "env_sha256": None,
                    "agent_version": 1,
                    "agent_name": "Agent",
                }
            ),
        )
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", _fake_jwt)
    real_prepare = prepare_app_access
    minted_ids: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["repository_ids"]
        minted_ids.append(ids)
        return (
            httpx.Response(404)
            if ids == [101]
            else httpx.Response(201, json={"token": "write-token"})
        )

    async def prepare_with_transport(*args: Any, **kwargs: Any):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await real_prepare(db_session_factory, client, **kwargs)

    delivered: list[str] = []

    async def add_credentials(*args: Any, **kwargs: Any) -> None:
        access = kwargs["access"]
        delivered.extend(token.token for token in access.tokens)

    async def rotate_resource(*args: Any, **kwargs: Any) -> None:
        delivered.append(str(args[3]))

    monkeypatch.setattr(github_app_session, "prepare_app_access", prepare_with_transport)
    monkeypatch.setattr(github_app_session, "add_app_credentials", add_credentials)
    monkeypatch.setattr(github_app_session, "rotate_session_repo_token", rotate_resource)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    rotated = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # external MA calls are stubbed above
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={"https://github.com/example/write": "resource"},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
    )
    assert delivered == ["write-token", "write-token"]
    assert rotated.excluded_repo_urls == (read_url,)
    assert rotated.token_repo_urls == (write_url,)
    async with db_session_factory() as session:
        rows = list(
            await session.scalars(
                select(GitHubIssuedToken).where(GitHubIssuedToken.agent_id == agent_id)
            )
        )
        assert sorted(row.status for row in rows) == ["delivered", "revoked"]
        job = await session.get(GitHubInstallationReconciliation, 771)
        assert job is not None and job.generation == 1
        current = await get_thread_session_by_id(session, id=mapped.id)
        assert current is not None and current.effective_config is not None
        assert current.effective_config.token_repo_urls == (write_url,)
        assert tuple(url for url, _ in current.effective_config.unavailable_repos) == (read_url,)
        assert current.effective_config.unavailable_notice_pending == (read_url,)
        first_refused = current.effective_config.unavailable_repos[0][1]

    minted_ids.clear()
    await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={write_url: "resource"},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
        unavailable_repos=((read_url, first_refused),),
    )
    assert minted_ids == [[102]]

    # A newly granted repo sharing the refused repo's profile is minted alone.
    async with db_session_factory.begin() as session:
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=103,
                owner_id=1,
                installation_id=771,
                repo_full_name="example/new-read",
                max_access="read",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
        await session.flush()
        await github_app_installations.upsert_github_app(
            session,
            installation_id=771,
            account_id=1,
            account_login="example",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        await github_access.stage_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=103,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=None,
            is_working_repo=False,
        )
    minted_ids.clear()
    newly_rotated = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={write_url: "resource"},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
        unavailable_repos=((read_url, first_refused),),
    )
    assert minted_ids == [[103], [102]]
    assert newly_rotated.excluded_repo_urls == (read_url,)
    async with db_session_factory() as session:
        current = await get_thread_session_by_id(session, id=mapped.id)
        assert current is not None and current.effective_config is not None
        assert current.effective_config.unavailable_repos == ((read_url, first_refused),)
        assert current.effective_config.unavailable_notice_pending == (read_url,)

    # Even after its 60 minutes, rotation leaves the refused repo to the
    # turn-boundary replacement and keeps the original refusal time.
    expired = first_refused - timedelta(minutes=61)
    minted_ids.clear()
    await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={write_url: "resource"},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
        unavailable_repos=((read_url, expired),),
    )
    assert minted_ids == [[103], [102]]


@pytest.mark.asyncio
async def test_two_refused_installations_leave_no_helper_or_replacement_loop(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory, repos=((771, 101, False), (772, 201, False))
    )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)
    minted: list[list[int]] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        minted.append(json.loads(request.content)["repository_ids"])
        return httpx.Response(422)

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        first = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="both-refused-first",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
        )
        assert minted == [[101], [201]]
        assert first.multi_installation
        assert first.tokens == first.repo_credential_map == ()
        minted.clear()
        second = await prepare_app_access(
            db_session_factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="both-refused-second",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
            excluded_repo_urls=first.excluded_repo_urls,
        )
    assert minted == []
    assert second.tokens == second.repo_credential_map == ()

    agent = ma_agent(id="agent_both_refused")
    now = datetime.now(UTC)
    recorded = desired_snapshot(
        agent,
        hidden_mcp_server_names=frozenset(),
        environment_id="environment",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id="vault",
    ).model_copy(
        update={"unavailable_repos": tuple((url, now) for url in first.excluded_repo_urls)}
    )
    desired = await desired_snapshot_for(
        db_session_factory,
        agent=agent,
        environment_id="environment",
        tenant_id=tenant_id,
        agent_uuid=agent_id,
        account_id=uuid.uuid4(),
        github_app=config,
        fernet=fernet,
        recorded=recorded,
        tool_safety=OPEN_TOOL_SAFETY,
        public_url=None,
    )
    assert desired.token_scopes == ()
    assert recorded.github_repo_map_sha256 is desired.github_repo_map_sha256 is None
    assert recorded.github_repo_script_sha256 is desired.github_repo_script_sha256 is None
    assert (
        decide_session_compatibility(
            recorded=recorded, desired=desired, capabilities=MaCapabilities(), now=now
        )
        == ReuseAsIs()
    )
    one_excluded = recorded.model_copy(
        update={"unavailable_repos": ((first.excluded_repo_urls[0], now),)}
    )
    one_desired = await desired_snapshot_for(
        db_session_factory,
        agent=agent,
        environment_id="environment",
        tenant_id=tenant_id,
        agent_uuid=agent_id,
        account_id=uuid.uuid4(),
        github_app=config,
        fernet=fernet,
        recorded=one_excluded,
        tool_safety=OPEN_TOOL_SAFETY,
        public_url=None,
    )
    assert len({scope[0] for scope in one_desired.token_scopes}) == 1
    assert one_desired.github_repo_map_sha256 == github_repo_map_sha256(
        (("owner772/repo201", "GH_TOKEN_I772_0"),)
    )
    assert one_desired.github_repo_script_sha256 == github_repo_script_sha256()


@pytest.mark.asyncio
async def test_refusal_does_not_hide_a_healthy_installation_map_expansion(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, agent_id = await _seed_multi_installation_grants(
        db_session_factory,
        repos=((771, 101, False), (772, 201, False), (773, 301, False)),
    )
    refused = "https://github.com/owner771/repo101"
    config = GithubAppSettings()
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    async def plan():
        return await effective_repo_plan(
            db_session_factory,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            config=config,
            fernet=fernet,
            excluded_repo_urls=(refused,),
        )

    before = await plan()
    assert before[3] == (
        ("owner772/repo201", "GH_TOKEN_I772_0"),
        ("owner773/repo301", "GH_TOKEN_I773_0"),
    )
    now = datetime.now(UTC)
    recorded = SessionSnapshot.model_validate(
        {
            "ma_agent_id": "agent_map",
            "model_id": "test-model",
            "system_sha256": None,
            "skills_sha256": "skills",
            "environment_id": "environment",
            "github_mode": "app",
            "repo_urls": (),
            "token_repo_urls": (
                "https://github.com/owner772/repo201",
                "https://github.com/owner773/repo301",
            ),
            "token_scopes": (
                (772, 0, (201,), int(now.timestamp()), str(uuid.uuid4())),
                (773, 0, (301,), int(now.timestamp()), str(uuid.uuid4())),
            ),
            "repo_token_issued_at": int(now.timestamp()),
            "token_issued_at_by_installation": {
                772: int(now.timestamp()),
                773: int(now.timestamp()),
            },
            "github_repo_map_sha256": github_repo_map_sha256(before[3]),
            "github_repo_script_sha256": "script",
            "unavailable_repos": ((refused, now),),
            "repo_url": None,
            "repo_branch": None,
            "memory_store_id": None,
            "vault_id": "vault",
            "tools_sha256": "tools",
            "mcp_servers_sha256": "servers",
            "env_sha256": None,
            "agent_version": 1,
            "agent_name": "Agent",
        }
    )
    unchanged = recorded.model_copy(update={"token_repo_urls": before[0]})
    assert (
        decide_session_compatibility(
            recorded=recorded, desired=unchanged, capabilities=MaCapabilities(), now=now
        )
        == ReuseAsIs()
    )

    async with db_session_factory.begin() as session:
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=202,
                owner_id=772,
                installation_id=772,
                repo_full_name="owner772/repo202",
                max_access="write",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
        await session.flush()
        await github_app_installations.upsert_github_app(
            session,
            installation_id=772,
            account_id=772,
            account_login="owner772",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        session.add(
            AgentGitHubGrant(
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=202,
                baseline_access="write",
                ceiling_access="write",
                mount_path=None,
                is_working_repo=False,
                app_visible=True,
                granted_by_account_id=None,
            )
        )
    after = await plan()
    assert after[3] == (
        ("owner772/repo201", "GH_TOKEN_I772_0"),
        ("owner772/repo202", "GH_TOKEN_I772_0"),
        ("owner773/repo301", "GH_TOKEN_I773_0"),
    )
    assert b"owner772/repo202\tGH_TOKEN_I772_0\n" in render_github_repo_map(after[3])
    actual_desired = await desired_snapshot_for(
        db_session_factory,
        agent=ma_agent(id="agent_map"),
        environment_id="environment",
        tenant_id=tenant_id,
        agent_uuid=agent_id,
        account_id=uuid.uuid4(),
        github_app=config,
        fernet=fernet,
        recorded=recorded,
        tool_safety=OPEN_TOOL_SAFETY,
        public_url=None,
    )
    assert actual_desired.github_repo_map_sha256 == github_repo_map_sha256(after[3])
    desired = recorded.model_copy(
        update={
            "token_repo_urls": after[0],
            "token_scopes": after[2],
            "github_repo_map_sha256": github_repo_map_sha256(after[3]),
        }
    )
    decision = decide_session_compatibility(
        recorded=recorded, desired=desired, capabilities=MaCapabilities(), now=now
    )
    assert isinstance(decision, ReplaceSession)
    assert "github_helper" in decision.reasons


def test_repos_refused_at_mint_do_not_replace_the_session_every_turn() -> None:
    from daimon.core.session_compat import identity_change_reasons
    from daimon.core.session_snapshot import SessionSnapshot

    base = SessionSnapshot.model_validate(
        {
            "ma_agent_id": "agent_compat",
            "model_id": "test-model",
            "system_sha256": None,
            "skills_sha256": "skills",
            "environment_id": "environment",
            "github_mode": "app",
            "repo_urls": [],
            "token_repo_urls": [],
            "repo_url": None,
            "repo_branch": None,
            "memory_store_id": None,
            "vault_id": "vault",
            "tools_sha256": "tools",
            "mcp_servers_sha256": "servers",
            "env_sha256": None,
            "agent_version": 1,
            "agent_name": "Agent",
        }
    )
    common = {
        "github_mode": "app",
        "repo_urls": ("https://github.com/acme/kept",),
        "repo_url": None,
        "repo_branch": None,
    }
    recorded = base.model_copy(
        update={
            **common,
            "token_repo_urls": ("https://github.com/acme/kept",),
            "unavailable_repos": (
                ("https://github.com/acme/refused", datetime(2026, 10, 16, 12, tzinfo=UTC)),
            ),
        }
    )
    still_covered = base.model_copy(
        update={
            **common,
            "token_repo_urls": ("https://github.com/acme/kept", "https://github.com/acme/refused"),
            "unavailable_repos": (),
        }
    )
    reconciled = base.model_copy(
        update={**common, "token_repo_urls": ("https://github.com/acme/kept",)}
    )
    newly_granted = base.model_copy(
        update={
            **common,
            "token_repo_urls": ("https://github.com/acme/kept", "https://github.com/acme/new"),
        }
    )

    now = recorded.unavailable_repos[0][1] + timedelta(minutes=30)
    assert "repo_set" not in identity_change_reasons(recorded, still_covered, now=now)
    assert "repo_set" not in identity_change_reasons(recorded, reconciled, now=now)
    assert "repo_set" in identity_change_reasons(recorded, newly_granted, now=now)
    assert "repo_set" in identity_change_reasons(
        recorded, still_covered, now=now + timedelta(minutes=30)
    )
    assert "repo_set" in identity_change_reasons(
        recorded.model_copy(update={"unavailable_repos": ()}), still_covered, now=now
    )

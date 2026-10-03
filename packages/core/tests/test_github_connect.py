"""Connection invitation and requester access properties."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core._models import Account, GitHubConnectInvitation, GitHubUserLink, Tenant
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.github_requester_access import (
    PermissionCache,
    effective_access,
    linked_permissions,
)
from daimon.core.stores import github_connect, github_links
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def test_effective_access_properties() -> None:
    levels = ("none", "read", "write")
    for baseline in levels:
        for ceiling in levels:
            for asker in levels:
                if levels.index(baseline) > levels.index(ceiling) or ceiling == "none":
                    continue
                result = effective_access({1: baseline}, {1: ceiling}, {1: asker}).get(1, "none")
                rank = levels.index(result)
                assert rank >= levels.index(baseline)
                assert rank <= levels.index(ceiling)
                assert rank == max(
                    levels.index(baseline), min(levels.index(ceiling), levels.index(asker))
                )
    assert effective_access({}, {1: "write"}, {}) == {}
    assert effective_access({1: "read"}, {}, {}) == {1: "read"}
    with pytest.raises(ValueError, match="baseline exceeds ceiling"):
        effective_access({1: "write"}, {1: "read"}, {1: "write"})


@pytest.mark.asyncio
async def test_invitation_is_admin_only_and_single_use(db_session: AsyncSession) -> None:
    tenant_id, admin_id, member_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=admin_id, tenant_id=tenant_id, role="admin"))
    db_session.add(Account(id=member_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    with pytest.raises(ValueError, match="tenant admin"):
        await github_connect.mint_invitation(
            db_session, tenant_id=tenant_id, requester_account_id=member_id
        )
    token = await github_connect.mint_invitation(
        db_session,
        tenant_id=tenant_id,
        requester_account_id=admin_id,
        workspace_label="Example workspace",
        requester_label="Alex",
    )
    invitation = await github_connect.get_invitation(db_session, github_connect.digest(token))
    assert invitation is not None and invitation.workspace_label == "Example workspace"
    assert invitation.requester_label == "Alex"
    assert invitation.expires_at > datetime.now(UTC) + timedelta(days=6)
    await github_connect.create_flow(
        db_session,
        invitation_hash=github_connect.digest(token),
        state="state",
        cookie="cookie",
        encrypted_verifier=b"encrypted",
    )
    assert await github_connect.get_flow(db_session, state="state", cookie="wrong") is None
    assert await github_connect.get_flow(db_session, state="state", cookie="cookie") is not None
    saved = await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=17,
        repos=[
            github_connect.RepoConfirmation(
                repo_id=101,
                owner_id=55,
                installation_id=77,
                full_name="example/repo",
                max_access="read",
            )
        ],
        orgs=[],
    )
    assert saved
    assert await github_connect.get_invitation(db_session, github_connect.digest(token)) is None
    assert not await github_connect.confirm(
        db_session, state="state", cookie="cookie", github_user_id=17, repos=[], orgs=[]
    )
    expired_token = await github_connect.mint_invitation(
        db_session, tenant_id=tenant_id, requester_account_id=admin_id
    )
    expired = await db_session.get(GitHubConnectInvitation, github_connect.digest(expired_token))
    assert expired is not None
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    assert (
        await github_connect.get_invitation(db_session, github_connect.digest(expired_token))
        is None
    )


@pytest.mark.asyncio
async def test_refresh_serializes_for_one_github_user(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    user_id = 98765
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=user_id,
                login="alex",
                encrypted_access_token=encrypt_token(fernet, "old"),
                encrypted_refresh_token=encrypt_token(fernet, "refresh-old"),
                access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                refresh_expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.path == "/login/oauth/access_token":
            refreshes += 1
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                json={
                    "access_token": "new",
                    "refresh_token": "refresh-new",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        assert request.headers["authorization"] == "Bearer new"
        return httpx.Response(
            200, json={"repositories": [{"id": 123, "permissions": {"pull": True}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cache = PermissionCache(ttl_seconds=0)
        left, right = await asyncio.gather(
            *(
                linked_permissions(
                    sessionmaker,
                    client,
                    user_id=user_id,
                    installation_id=88,
                    fernet=fernet,
                    client_id="client",
                    client_secret="secret",
                    cache=cache,
                )
                for _ in range(2)
            )
        )
    assert left == right == {123: "read"}
    assert refreshes == 1
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=user_id)
    assert row is not None and row.token_generation == 2 and row.status == "active"

"""New-repo connection authority must not depend on an earlier chat turn."""

from __future__ import annotations

import uuid

import pytest
from daimon.core._models import Account, Tenant
from daimon.core.github_panel import sync_connect_admin
from daimon.core.stores.github_connect import admin_account_for_platform_user
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_new_platform_admin_resolves_without_admission(
    db_session: AsyncSession, platform: str
) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform=platform, external_id="workspace"))
    await db_session.flush()
    # These are the consecutive core calls in github_new_repo's connect action.
    # No turn admission or /github connect command has written a role snapshot.
    await sync_connect_admin(
        db_session,
        tenant_id=tenant_id,
        platform=platform,
        platform_user_id="new-admin",
        is_platform_admin=True,
    )
    account_id = await admin_account_for_platform_user(
        db_session, tenant_id=tenant_id, external_id="new-admin", is_platform_admin=True
    )
    account = await db_session.get(Account, account_id)
    assert account is not None and account.role == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "platform_admin"), [("user", True), ("user", False), ("admin", False)]
)
async def test_new_repo_lookup_queries_a_fresh_platform_principal(
    role: str,
    platform_admin: bool,
) -> None:
    # Run the production relational query without Postgres or an admission turn.
    from typing import Any, cast
    from unittest.mock import AsyncMock

    from sqlalchemy import create_engine, text
    from sqlalchemy.sql import Select

    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE tenants (id CHAR(32), platform TEXT, archived_at TEXT)")
        )
        connection.execute(
            text("CREATE TABLE accounts (id CHAR(32), tenant_id CHAR(32), role TEXT)")
        )
        connection.execute(
            text(
                "CREATE TABLE platform_principals "
                "(account_id CHAR(32), tenant_id CHAR(32), platform TEXT, external_id TEXT, foreign_home_id TEXT)"
            )
        )
        connection.execute(
            text("INSERT INTO tenants VALUES (:id, 'discord', NULL)"), {"id": tenant_id.hex}
        )
        connection.execute(
            text("INSERT INTO accounts VALUES (:id, :tenant, :role)"),
            {"id": account_id.hex, "tenant": tenant_id.hex, "role": role},
        )
        connection.execute(
            text(
                "INSERT INTO platform_principals VALUES (:id, :tenant, 'discord', 'new-admin', NULL)"
            ),
            {"id": account_id.hex, "tenant": tenant_id.hex},
        )
        session = AsyncMock(spec=AsyncSession)

        def scalar(statement: Select[Any]) -> object:
            return connection.execute(statement).scalar()

        session.scalar.side_effect = scalar
        if platform_admin or role == "admin":
            assert (
                await admin_account_for_platform_user(
                    cast(AsyncSession, session),
                    tenant_id=tenant_id,
                    external_id="new-admin",
                    is_platform_admin=platform_admin,
                )
                == account_id
            )
            if role == "admin":
                # The operator CLI omits the verdict and needs no platform credentials.
                assert (
                    await admin_account_for_platform_user(
                        cast(AsyncSession, session), tenant_id=tenant_id, external_id="new-admin"
                    )
                    == account_id
                )
        else:
            with pytest.raises(ValueError, match="workspace admin"):
                await admin_account_for_platform_user(
                    cast(AsyncSession, session),
                    tenant_id=tenant_id,
                    external_id="new-admin",
                    is_platform_admin=platform_admin,
                )
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_admin", [True, False])
async def test_new_repo_invitation_lifecycle_uses_live_platform_authority(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, platform_admin: bool
) -> None:

    from cryptography.fernet import Fernet
    from daimon.core._models import DaimonAdmin, GitHubConnectInvitation, PlatformPrincipal
    from daimon.core.admin_authority import AdminAuthority
    from daimon.core.config import Settings
    from daimon.core.github_panel import connect_link
    from daimon.core.stores import github_connect
    from pydantic import SecretStr

    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id="workspace"))
    await db_session.flush()
    db_session.add(Account(id=account_id, tenant_id=tenant_id, role="user"))
    await db_session.flush()
    db_session.add_all(
        [
            PlatformPrincipal(
                tenant_id=tenant_id,
                platform="discord",
                external_id="new-admin",
                account_id=account_id,
            ),
            # A real delegated grant must never substitute for native authority.
            DaimonAdmin(
                tenant_id=tenant_id,
                platform="discord",
                subject_kind="user",
                subject_id="new-admin",
                granted_by="owner",
            ),
        ]
    )
    await db_session.flush()
    settings = Settings.model_validate(
        dict(
            database={"url": "postgresql+asyncpg://localhost/daimon_test"},
            crypto={"keys": [SecretStr(Fernet.generate_key().decode())]},
            github_app={
                "app_id": "app",
                "app_slug": "app",
                "private_key": "key",
                "client_id": "client",
                "client_secret": "secret",
            },
            mcp={"public_url": "https://example.invalid/mcp"},
        )
    )
    from unittest.mock import AsyncMock

    authority = AsyncMock(
        return_value=AdminAuthority(platform_admin=platform_admin, daimon_admin=True)
    )
    monkeypatch.setattr(github_connect, "resolve_authority", authority)
    if not platform_admin:
        with pytest.raises(ValueError, match="admin can connect GitHub"):
            await connect_link(
                db_session,
                settings=settings,
                tenant_id=tenant_id,
                platform="discord",
                platform_user_id="new-admin",
                verified_tenant_admin=False,
            )
        with pytest.raises(ValueError, match="tenant admin"):
            await github_connect.mint_invitation(
                db_session,
                tenant_id=tenant_id,
                requester_account_id=account_id,
                verified_platform_admin=False,
            )
        return
    url = await connect_link(
        db_session,
        settings=settings,
        tenant_id=tenant_id,
        platform="discord",
        platform_user_id="new-admin",
        verified_tenant_admin=True,
    )
    token_hash = github_connect.digest(url.rsplit("/", 1)[-1])
    invitation = await github_connect.get_invitation(db_session, token_hash, settings=settings)
    assert invitation is not None and invitation.requester_account_id == account_id
    assert (await github_connect.invitation_status(db_session, token_hash, settings=settings))[
        0
    ] == "active"
    await github_connect.create_flow(
        db_session,
        invitation_hash=token_hash,
        state="state",
        cookie="cookie",
        encrypted_verifier=b"verifier",
    )
    # Losing platform authority invalidates the open link, despite the live grant.
    authority.return_value = AdminAuthority(platform_admin=False, daimon_admin=True)
    assert await github_connect.get_invitation(db_session, token_hash, settings=settings) is None
    assert (await github_connect.invitation_status(db_session, token_hash, settings=settings))[
        0
    ] == "requester_left"
    assert not await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=1,
        repos=[
            github_connect.RepoConfirmation(
                repo_id=101,
                owner_id=55,
                installation_id=77,
                full_name="example/new",
                max_access="write",
            )
        ],
        verified_platform_admin=False,
    )
    # Restored live platform authority can save without ever writing accounts.role.
    authority.return_value = AdminAuthority(platform_admin=True, daimon_admin=True)
    assert await github_connect.confirm(
        db_session,
        state="state",
        cookie="cookie",
        github_user_id=1,
        repos=[
            github_connect.RepoConfirmation(
                repo_id=101,
                owner_id=55,
                installation_id=77,
                full_name="example/new",
                max_access="write",
            )
        ],
        verified_platform_admin=True,
    )
    row = await db_session.get(GitHubConnectInvitation, token_hash)
    assert row is not None and row.used_at is not None
    account = await db_session.get(Account, account_id)
    assert account is not None and account.role == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_admin", [True, False])
@pytest.mark.parametrize("delegated_admin", [True, False])
@pytest.mark.parametrize("principal_kind", ["host", "missing", "foreign"])
async def test_browser_link_requires_live_platform_authority_for_a_user_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    platform_admin: bool,
    delegated_admin: bool,
    principal_kind: str,
) -> None:
    from typing import cast
    from unittest.mock import AsyncMock, MagicMock

    from daimon.core._models import PlatformPrincipal
    from daimon.core.admin_authority import AdminAuthority
    from daimon.core.config import Settings
    from daimon.core.stores import github_connect

    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    authority = AsyncMock(
        return_value=AdminAuthority(
            platform_admin=platform_admin,
            daimon_admin=delegated_admin,
        )
    )
    monkeypatch.setattr(github_connect, "resolve_authority", authority)
    session = AsyncMock(spec=AsyncSession)
    session.bind = None
    result = MagicMock()
    result.scalar_one_or_none.return_value = (
        None
        if principal_kind == "missing"
        else PlatformPrincipal(
            account_id=account_id,
            tenant_id=tenant_id,
            platform="discord",
            external_id="new-admin",
            foreign_home_id="other-workspace" if principal_kind == "foreign" else None,
        )
    )
    session.execute.return_value = result
    account = Account(id=account_id, tenant_id=tenant_id, role="user")
    assert await github_connect._may_hold_link(  # pyright: ignore[reportPrivateUsage]
        cast(AsyncSession, session),
        account,
        tenant_id=tenant_id,
        agent_id=None,
        settings=Settings.model_validate(
            {"database": {"url": "postgresql+asyncpg://localhost/daimon_test"}}
        ),
    ) is (platform_admin and principal_kind == "host")
    if principal_kind != "host":
        authority.assert_not_awaited()
    assert account.role == "user"

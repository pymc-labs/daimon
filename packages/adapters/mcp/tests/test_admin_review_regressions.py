"""Cross-boundary regressions for live delegation and registered chat credentials."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.auth.verifier import CHAT_EXECUTION_CLAIM, DaimonJWTVerifier
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.tools._admin import current_admin
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._ctx import _require_platform_admin
from daimon.adapters.mcp.tools._session_access import (
    _hub_reads_as_anyone,
    hub_session_access,
    load_hub_subject,
    session_belongs_to_caller,
)
from daimon.adapters.mcp.tools.admins import change_admin
from daimon.core.admin_authority import resolve_authority
from daimon.core.config import DiscordSettings
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_SEALED,
)
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.github_panel import sync_connect_admin
from daimon.core.mcp_auth import mint_agent_mcp_token, mint_chat_mcp_token
from daimon.core.membership import Membership
from daimon.core.operator_tokens import OperatorTokenError
from daimon.core.panel_operator_tokens import mint_panel_operator_token
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.daimon_admins import change_grant
from daimon.core.stores.domain import Role
from daimon.testing import ma_session
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_platform_principal, make_tenant
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SECRET = b"chat-integration-secret-at-least-32-bytes"


@pytest.mark.parametrize("kind", ["member", "delegate", "platform"])
async def test_real_chat_mint_verifier_middleware_preserves_human_authority(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="123456789012345678")
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id="223456789012345678"
    )
    if kind == "platform":
        await set_role(db_session, principal.account_id, Role.ADMIN)
    if kind == "delegate":
        await change_grant(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            subject_kind="user",
            subject_id=principal.external_id,
            actor_user_id="owner",
            is_platform_admin=True,
        )
    agent_id = uuid.uuid4()
    token, _, _ = await mint_chat_mcp_token(
        db_session,
        account_id=principal.account_id,
        agent_id=agent_id,
        secret=SECRET,
        now=datetime.now(UTC),
    )
    await db_session.commit()
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=db_session_factory)
    access = await verifier.verify_token(token)
    assert access is not None and access.claims[CHAT_EXECUTION_CLAIM] is True
    assert "agent_id" not in access.claims
    monkeypatch.setattr(
        DiscordRestMembershipResolver,
        "resolve",
        AsyncMock(return_value=Membership(is_member=True, is_admin=kind == "platform")),
    )
    settings = MagicMock(discord=DiscordSettings(bot_token=SecretStr("test")))
    mcp = FastMCP(name="real-chat", auth=verifier)
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=production_subject_resolver,
            tenant_resolver=production_tenant_resolver,
            role_resolver=production_role_resolver,
            agent_id_resolver=production_agent_id_resolver,
            is_admin_resolver=production_is_admin_resolver,
            internal_resolver=production_internal_resolver,
            sessionmaker=db_session_factory,
            settings=settings,
        )
    )
    observed: list[AuthIdentity] = []

    @mcp.tool
    async def inspect_chat(ctx: Context) -> bool:
        auth = await ctx.get_state("auth")
        assert isinstance(auth, AuthIdentity)
        observed.append(auth)
        assert auth.is_chat_execution and auth.chat_agent_id == agent_id
        assert auth.agent_id is None and not auth.is_external_agent
        assert not mcp_subject(auth).via_agent_key
        async with db_session_factory() as db:
            assert await current_admin(db, auth) is (kind != "member")
        runtime = SimpleNamespace(session_factory=db_session_factory, settings=settings)
        if kind == "platform":
            await change_admin(runtime, auth, subject_id="new-admin")
            _require_platform_admin(auth)
        else:
            with pytest.raises(ToolError, match="Only platform admins"):
                await change_admin(runtime, auth, subject_id="new-admin")
            with pytest.raises(ToolError):
                _require_platform_admin(auth)
        return auth.is_admin

    result = await call_mcp_tool(mcp.http_app(), token=token, name="inspect_chat")
    assert "error" not in result and observed
    assert observed[-1].is_admin is (kind != "member")
    assert observed[-1].is_platform_admin is (kind == "platform")
    account = await get_account(db_session, principal.account_id)
    assert account is not None and account.role is (Role.ADMIN if kind == "platform" else Role.USER)


async def test_external_key_cannot_forge_chat_or_drop_its_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id="member"
    )
    token = await mint_agent_mcp_token(
        db_session,
        account_id=principal.account_id,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        label="external",
        secret=SECRET,
        now=datetime.now(UTC),
    )
    await db_session.commit()
    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=db_session_factory)
    access = await verifier.verify_token(token)
    assert access is not None and access.claims[CHAT_EXECUTION_CLAIM] is False
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    for malformed in [None, "not-a-uuid"]:
        changed = dict(claims)
        if malformed is None:
            changed.pop("agent_id")
        else:
            changed["agent_id"] = malformed
        changed[CHAT_EXECUTION_CLAIM] = True
        assert await verifier.verify_token(jwt.encode(changed, SECRET, algorithm="HS256")) is None
    claims[CHAT_EXECUTION_CLAIM] = True
    verified = await verifier.verify_token(jwt.encode(claims, SECRET, algorithm="HS256"))
    assert verified is not None and verified.claims[CHAT_EXECUTION_CLAIM] is False


async def test_panel_role_delegate_never_acquires_hub_sealed_transcript_exception(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="123456789012345678")
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="discord", external_id="223456789012345678"
    )
    key = dict(
        tenant_id=tenant.id,
        platform="discord",
        subject_kind="role",
        subject_id="323456789012345678",
        actor_user_id="owner",
        is_platform_admin=True,
    )
    await change_grant(db_session, **key)
    await db_session.commit()
    facts = AsyncMock(return_value=Membership(is_member=True, role_ids=(key["subject_id"],)))
    monkeypatch.setattr(DiscordRestMembershipResolver, "resolve", facts)
    settings = MagicMock(discord=DiscordSettings(bot_token=SecretStr("test")))
    runtime = SimpleNamespace(session_factory=db_session_factory)
    auth = AuthIdentity(
        account_id=principal.account_id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id=principal.external_id,
    )
    sealed = ma_session(
        metadata={
            MA_METADATA_KEY_ACCOUNT: str(uuid.uuid4()),
            MA_METADATA_KEY_CHANNEL: "channel",
            MA_METADATA_KEY_SEALED: "channel",
        }
    )
    for stage in ("panel", "role-loss", "revoke"):
        if stage == "role-loss":
            facts.return_value = Membership(is_member=True)
        if stage == "revoke":
            facts.return_value = Membership(is_member=True, role_ids=(key["subject_id"],))
            await change_grant(db_session, **key, revoke=True)
            await db_session.commit()
        authority = await resolve_authority(
            db_session_factory,
            settings,
            tenant_id=tenant.id,
            platform_user_id=principal.external_id,
        )
        assert authority.daimon_admin is (stage == "panel")
        if stage == "panel":
            await sync_connect_admin(
                db_session,
                tenant_id=tenant.id,
                platform="discord",
                platform_user_id=principal.external_id,
                verified_tenant_admin=authority.daimon_admin,
            )
            with pytest.raises(OperatorTokenError, match="platform admin"):
                await mint_panel_operator_token(
                    db_session,
                    tenant_id=tenant.id,
                    platform="discord",
                    platform_user_id=principal.external_id,
                    is_platform_admin=False,
                    scopes=["tenant:read"],
                    label=None,
                    secret=SECRET,
                    now=datetime.now(UTC),
                )
            await db_session.commit()
        account = await get_account(db_session, principal.account_id)
        assert account is not None and account.role is Role.USER and not account.platform_role_ids
        subject = await load_hub_subject(runtime, auth)
        assert not subject.is_admin
        assert not _hub_reads_as_anyone(subject, sealed.metadata)
        with hub_session_access(subject, "read"):
            assert not session_belongs_to_caller(sealed, auth)

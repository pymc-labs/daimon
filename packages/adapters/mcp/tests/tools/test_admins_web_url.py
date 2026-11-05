"""grant_admin links to the granted person's page in the web app, when they have one."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.admins import granted
from daimon.core.config import WebSettings
from daimon.core.stores.domain import Role
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_WEB = WebSettings(public_url="https://app.example.com")  # type: ignore[call-arg]


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], web: WebSettings | None) -> McpRuntime:
    return cast(
        McpRuntime, SimpleNamespace(session_factory=sessionmaker, settings=SimpleNamespace(web=web))
    )


async def test_a_granted_user_with_a_web_person_links_to_their_admin_page(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord")
        # Links follow the caller's web role: the admin has no web person yet, so their
        # chat admin standing counts, while the granted user is a member on the web.
        admin = await make_account(session, tenant=tenant)
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform="discord", external_id="U42", tenant=tenant, account=account
        )
        person = await make_web_person(session, tenant=tenant, account=account)
    auth = AuthIdentity(
        account_id=admin.id,
        tenant_id=tenant.id,
        role=Role.ADMIN,
        platform="discord",
        platform_user_id="U1",
        is_admin=True,
    )
    runtime = _runtime(committing_sessionmaker, _WEB)

    result = await granted(runtime, auth, subject_id="U42", subject_kind="user")
    assert result.web_url == (f"https://app.example.com/w/{tenant.id}/admin?person={person.id}"), (
        "the person's own page"
    )
    stranger = await granted(runtime, auth, subject_id="U99", subject_kind="user")
    assert stranger.web_url is None, "no person on the web, no link"
    role = await granted(runtime, auth, subject_id="U42", subject_kind="role")
    assert role.web_url is None, "a role has no page"
    plain = await granted(
        _runtime(committing_sessionmaker, None), auth, subject_id="U42", subject_kind="user"
    )
    assert plain.web_url is None, "no web app, no link"

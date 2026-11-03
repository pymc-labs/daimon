"""The integrations views: admin only, scoped to the workspace, and free of credentials."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types.beta.vaults.beta_managed_agents_mcp_oauth_auth_response import (
    BetaManagedAgentsMCPOAuthAuthResponse,
)
from daimon.core import integration_views as views
from daimon.core._models import (
    Account,
    AgentGitHubGrant,
    GitHubAccessRequest,
    GitHubInstallationReconciliation,
    GitHubPushResync,
    TenantGitHubRepo,
)
from daimon.core.authz import Subject
from daimon.core.credential_requests import mint_request_token
from daimon.core.integration_views import AgentServers, IntegrationsRefused
from daimon.core.stores import credential_requests, github_app_installations, mcp_oauth_flows
from daimon.core.stores.agent_mcp_credentials import upsert_credential
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

NOW = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
ADMIN = Subject(is_admin=True, platform_user_id="U1")
MEMBER = Subject(is_admin=False, platform_user_id="U2")
SECRET = b"token-that-must-never-show"
URL = "https://mcp.example.com/sse"


async def _signed_in(
    db: AsyncSession,
    tenant: Any,
    agent_id: uuid.UUID,
    url: str,
    *,
    account_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A finished OAuth sign-in by a new person (or `account`) to `url` on one agent."""
    account_id = account_id or (await make_account(db, tenant=tenant)).id
    request = await credential_requests.create_credential_request(
        db,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=account_id,
        target="docs",
        mcp_server_url=url,
        requester_platform_user_id="requester",
        channel_id="chan",
        expires_at=NOW + dt.timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="daimon",
        requested_work=None,
    )
    state = uuid.uuid4().hex
    await mcp_oauth_flows.create_flow(
        db,
        state=state,
        request_token=request.token,
        tenant_id=tenant.id,
        account_id=account_id,
        agent_id=agent_id,
        server_name="docs",
        mcp_server_url=url,
        redirect_uri="https://x.example.com/cb",
        code_verifier="v",
        expires_at=NOW + dt.timedelta(hours=1),
    )
    await mcp_oauth_flows.mark_flow_completed(db, state=state, now=NOW)
    return account_id


async def _github(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """One shared repo and one agent's own, a working repo, failed syncs and a request."""
    admin, asker, agent, other = (uuid.uuid4() for _ in range(4))
    db.add_all(
        [
            Account(id=admin, tenant_id=tenant_id, role="admin"),
            Account(id=asker, tenant_id=tenant_id),
        ]
    )
    await db.flush()
    await github_app_installations.upsert(
        db, installation_id=9, account_login="team-a", repo_full_names=["team-a/app", "team-a/docs"]
    )
    for repo_id, name, scope in ((1, "team-a/app", None), (2, "team-a/docs", agent)):
        db.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=repo_id,
                scope_agent_id=scope,
                owner_id=5,
                installation_id=9,
                repo_full_name=name,
                max_access="write",
                authorized_by_github_user_id=17,
                authorized_by_account_id=admin,
            )
        )
    await db.flush()
    db.add_all(
        [
            AgentGitHubGrant(
                tenant_id=tenant_id,
                agent_id=agent,
                repo_id=repo_id,
                baseline_access="read",
                ceiling_access="write",
                is_working_repo=repo_id == 2,
            )
            for repo_id in (1, 2)
        ]
    )
    db.add(
        GitHubPushResync(
            repo_full_name="team-a/app",
            ref="refs/heads/main",
            delivery_id="d1",
            attempts=3,
            last_error="2 binding sync(s) failed",
        )
    )
    db.add(
        GitHubPushResync(
            repo_full_name="elsewhere/repo", ref="refs/heads/main", delivery_id="d2", last_error="x"
        )
    )
    db.add(GitHubInstallationReconciliation(installation_id=9, last_error="HTTPError: secret"))
    db.add(
        GitHubAccessRequest(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            requester_account_id=asker,
            requester_platform_user_id="U9",
            platform="discord",
            parent_channel_id="c",
            thread_id="t",
            agent_id=other,
            ma_agent_id="ag_other",
            agent_name="Other",
            repo_names=["team-a/docs"],
            expires_at=NOW + dt.timedelta(days=1),
        )
    )
    await db.flush()
    return agent, other


async def test_github_overview_lists_repos_per_agent_requests_and_failed_syncs(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    agent, other = await _github(db_session, tenant.id)

    overview = await views.load_github_overview(
        db_session, tenant_id=tenant.id, subject=ADMIN, agent_ids=[agent, other], now=NOW
    )

    (installation,) = overview.installations
    assert (installation.account_login, installation.repos) == ("team-a", 2)
    by_agent = {a.agent_id: a.repos for a in overview.agents}
    assert [(r.full_name, r.scope, r.working) for r in by_agent[agent]] == [
        ("team-a/app", "shared", False),
        ("team-a/docs", "agent", True),
    ]
    assert by_agent[other] == ()
    assert [r.agent_id for r in overview.requests] == [other]
    assert sorted((t.kind, t.subject) for t in overview.trouble) == [
        ("installation", "team-a"),
        ("push", "team-a/app refs/heads/main"),
    ], "another workspace's repo sync is not listed"


async def test_a_member_or_an_agent_key_is_refused_every_view(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    keyed = Subject(is_admin=True, platform_user_id="U1", via_agent_key=True)
    for subject in (MEMBER, keyed):
        with pytest.raises(IntegrationsRefused):
            await views.load_github_overview(
                db_session, tenant_id=tenant.id, subject=subject, agent_ids=[], now=NOW
            )
        with pytest.raises(IntegrationsRefused):
            await views.load_mcp_catalogue(
                db_session, tenant_id=tenant.id, subject=subject, agents=[]
            )
        with pytest.raises(IntegrationsRefused):
            await views.load_workspace_install(
                db_session, tenant_id=tenant.id, subject=subject, workspace_name="Acme", now=NOW
            )


async def test_the_chat_install_reads_the_tenant(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    install = await views.load_workspace_install(
        db_session, tenant_id=tenant.id, subject=ADMIN, workspace_name="Acme", now=NOW
    )
    assert install is not None
    assert (install.platform, install.name, install.status) == ("discord", "Acme", "connected")
    assert install.problem is False


async def test_the_catalogue_groups_agents_by_server_and_never_reads_a_token(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    first, second = uuid.uuid4(), uuid.uuid4()
    await upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=first,
        mcp_server_url=URL,
        encrypted_token=SECRET,
    )
    for _ in range(2):
        await _signed_in(db_session, tenant, second, URL + "/")

    catalogue = await views.load_mcp_catalogue(
        db_session,
        tenant_id=tenant.id,
        subject=ADMIN,
        agents=[
            AgentServers(first, "First", (("docs", URL),)),
            AgentServers(second, "Second", (("Docs", URL + "/"),)),
        ],
    )

    (server,) = catalogue
    assert server.name == "docs" and server.signed_in == 2
    by_agent = {a.name: a for a in server.agents}
    assert (by_agent["First"].shared_token, by_agent["First"].signed_in) == (True, 0)
    assert (by_agent["Second"].shared_token, by_agent["Second"].signed_in) == (False, 2)
    assert SECRET.decode() not in repr(catalogue)


def _grant(url: str, *, expires: dt.datetime | None, refresh: bool) -> Any:
    payload: dict[str, Any] = {
        "type": "mcp_oauth",
        "mcp_server_url": url,
        "expires_at": expires,
    }
    if refresh:
        payload["refresh"] = {
            "client_id": "client",
            "token_endpoint": "https://auth.example.com/token",
            "token_endpoint_auth": {"type": "none"},
        }
    auth = BetaManagedAgentsMCPOAuthAuthResponse.model_validate(payload)
    return SimpleNamespace(auth=auth)


async def test_expiry_names_a_lapsed_sign_in_with_nothing_to_renew_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session)
    agent = uuid.uuid4()
    accounts = [await make_account(db_session, tenant=tenant) for _ in range(3)]
    people = [a.id for a in accounts]
    past, future = NOW - dt.timedelta(days=1), NOW + dt.timedelta(days=1)
    credentials = {
        people[0]: _grant(URL, expires=past, refresh=False),
        people[1]: _grant(URL, expires=past, refresh=True),
        people[2]: _grant(URL, expires=future, refresh=False),
    }
    for account in accounts:
        await _signed_in(db_session, tenant, agent, URL, account_id=account.id)

    vaults = {
        f"daimon-mcp:{account}:{agent}": SimpleNamespace(
            id=f"vlt_{i}",
            display_name=f"daimon-mcp:{account}:{agent}",
            archived_at=None,
            created_at=NOW,
        )
        for i, account in enumerate(people)
    }
    by_vault = {v.id: credentials[uuid.UUID(v.display_name.split(":")[1])] for v in vaults.values()}

    async def list_vaults(client: Any, *, scope: Any) -> AsyncIterator[Any]:
        for vault in vaults.values():
            yield vault

    async def list_credentials(client: Any, vault_id: str, *, scope: Any) -> AsyncIterator[Any]:
        yield by_vault[vault_id]

    monkeypatch.setattr(views, "list_vaults", list_vaults)
    monkeypatch.setattr(views, "list_credentials", list_credentials)

    lapsed, complete = await views.check_mcp_expiry(
        object(),  # pyright: ignore[reportArgumentType]
        db_session,
        tenant_id=tenant.id,
        subject=ADMIN,
        server_urls=[URL],
        now=NOW,
    )

    assert complete
    assert [(e.account_id, e.agent_id) for e in lapsed] == [(people[0], agent)]
    with pytest.raises(IntegrationsRefused):
        await views.check_mcp_expiry(
            object(),  # pyright: ignore[reportArgumentType]
            db_session,
            tenant_id=tenant.id,
            subject=MEMBER,
            server_urls=[URL],
            now=NOW,
        )

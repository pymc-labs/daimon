"""One installation whose grants need two tokens gets a variable per token and the helper.

A read group and a write group in one GitHub organization mint two tokens.
GH_TOKEN holds only the working repo's, so the session also gets one
`GH_TOKEN_I<installation>_<n>` variable per token and the `github-repo.sh`
map, as several installations do. The planner (desired snapshot), the
preparation and the rollback rebuild must name the same variables, or the
session would be replaced every turn. One token keeps the GH_TOKEN layout.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace
from typing import Any, Literal, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.vaults.beta_managed_agents_environment_variable_auth_response import (
    BetaManagedAgentsEnvironmentVariableAuthResponse,
)
from cryptography.fernet import Fernet
from daimon.core import github_app_session
from daimon.core._models import AgentGitHubGrant, Tenant, TenantGitHubRepo
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import (
    AppSessionAccess,
    _current_app_access,  # pyright: ignore[reportPrivateUsage]
    add_app_credentials,
    effective_repo_plan,
    finish_app_delivery,
    prepare_app_access,
    repo_credential_entries,
    uses_per_token_credentials,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_requester_access import PermissionCache
from daimon.core.stores import github_app_installations
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

FERNET = build_multifernet((Fernet.generate_key().decode(),))
CONFIG = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))

type Grant = tuple[int, str, bool, Literal["read", "write"]]

READ_AND_WRITE: tuple[Grant, ...] = (
    (101, "acme/app", True, "write"),
    (103, "acme/docs", False, "read"),
)
WRITE_ONLY: tuple[Grant, ...] = (
    (101, "acme/app", True, "write"),
    (102, "acme/infra", False, "write"),
)


async def _seed(
    factory: async_sessionmaker[AsyncSession], grants: tuple[Grant, ...]
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for repo_id, name, _, _ in grants:
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=771,
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
            account_id=771,
            account_login="acme",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        for repo_id, _, working, access in grants:
            session.add(
                AgentGitHubGrant(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_id=repo_id,
                    baseline_access=access,
                    ceiling_access=access,
                    mount_path=None,
                    is_working_repo=working,
                    granted_by_account_id=None,
                )
            )
    return tenant_id, agent_id


def _github(request: httpx.Request) -> httpx.Response:
    ids = json.loads(request.content)["repository_ids"]
    return httpx.Response(201, json={"token": f"ghs_{'_'.join(map(str, ids))}"})


async def _prepare(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    grants: tuple[Grant, ...],
) -> tuple[AppSessionAccess, uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = await _seed(factory, grants)
    monkeypatch.setattr(github_app_session, "build_app_jwt", lambda *_a, **_k: "jwt")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_github)) as client:
        access = await prepare_app_access(
            factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="pending:single",
            config=CONFIG,
            fernet=FERNET,
            cache=PermissionCache(),
        )
        await finish_app_delivery(
            factory,
            client,
            access=access,
            provisional_session_id="pending:single",
            session_id="sesn_single",
        )
    return access, tenant_id, agent_id


class _Vault:
    def __init__(self) -> None:
        self.env: dict[str, str] = {}
        self.copilot: list[str] = []

    async def list_credentials(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        for name in list(self.env):
            yield SimpleNamespace(
                id=name,
                auth=BetaManagedAgentsEnvironmentVariableAuthResponse.model_validate(
                    {
                        "type": "environment_variable",
                        "secret_name": name,
                        "networking": {"type": "limited", "allowed_hosts": ["github.com"]},
                        "injection_location": {"header": True, "body": False},
                    }
                ),
            )

    async def store_credential(
        self, _client: Any, _vault_id: str, payload: Mapping[str, Any], **_kwargs: Any
    ) -> None:
        self.env[payload["auth"]["secret_name"]] = payload["auth"]["secret_value"]

    async def add_github_copilot_credential(
        self, _client: Any, *, token: str, **_kwargs: Any
    ) -> None:
        self.copilot.append(token)


async def _vault_after(monkeypatch: pytest.MonkeyPatch, access: AppSessionAccess) -> _Vault:
    vault = _Vault()
    for name in ("list_credentials", "store_credential", "add_github_copilot_credential"):
        monkeypatch.setattr(github_app_session, name, getattr(vault, name))
    await add_app_credentials(
        cast(AsyncAnthropic, SimpleNamespace()), vault_id="vlt", access=access
    )
    return vault


def test_per_token_layout_needs_two_tokens_or_two_installations() -> None:
    assert not uses_per_token_credentials(1, multi_installation=False)
    assert uses_per_token_credentials(2, multi_installation=False)
    assert uses_per_token_credentials(1, multi_installation=True)
    assert not uses_per_token_credentials(
        github_app_session.MAX_SINGLE_INSTALLATION_HELPER_GROUPS + 1, multi_installation=False
    )


async def test_read_and_write_groups_get_a_variable_each_and_the_helper_map(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access, tenant_id, agent_id = await _prepare(db_session_factory, monkeypatch, READ_AND_WRITE)
    assert not access.multi_installation
    assert access.per_token_credentials
    assert repo_credential_entries(access) == (
        ("acme/app", "GH_TOKEN_I771_1"),
        ("acme/docs", "GH_TOKEN_I771_0"),
    ), "read sorts before write, as the tokens are minted"

    vault = await _vault_after(monkeypatch, access)
    assert vault.env == {
        "GH_TOKEN_I771_0": "ghs_103",
        "GH_TOKEN_I771_1": "ghs_101",
        "GH_TOKEN": "ghs_101",
    }, "GH_TOKEN stays the working repo's token"

    # The desired snapshot names the same variables, so the session is stable.
    plan = await effective_repo_plan(
        db_session_factory,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        config=CONFIG,
        fernet=FERNET,
    )
    assert plan[2] == (), "token identity keeps the single-installation shape"
    assert plan[3] == repo_credential_entries(access)

    # The rollback rebuild gives each token its own variable again.
    rebuilt = await _current_app_access(
        db_session_factory,
        session_id="sesn_single",
        tenant_id=tenant_id,
        agent_id=agent_id,
        fernet=FERNET,
    )
    assert {(t.credential_name, t.token) for t in rebuilt.tokens} == {
        ("GH_TOKEN_I771_0", "ghs_103"),
        ("GH_TOKEN_I771_1", "ghs_101"),
    }
    assert repo_credential_entries(rebuilt) == repo_credential_entries(access)


async def test_one_token_keeps_the_gh_token_layout(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access, tenant_id, agent_id = await _prepare(db_session_factory, monkeypatch, WRITE_ONLY)
    assert len(access.tokens) == 1
    assert not access.per_token_credentials
    assert repo_credential_entries(access) == ()
    vault = await _vault_after(monkeypatch, access)
    assert vault.env == {"GH_TOKEN": "ghs_101_102"}
    assert vault.copilot == ["ghs_101_102"]
    plan = await effective_repo_plan(
        db_session_factory,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        config=CONFIG,
        fernet=FERNET,
    )
    assert plan[3] == ()

"""Every way an agent reaches GitHub gets the token of the repo's own installation.

A contract test, written after an agent with grants in two GitHub organizations
got 404s from its `github` MCP tool for the second one (2026-11-06): that tool
held only the working repo's token, while `gh` and the shell helper were right.
Nothing listed the ways a session reaches GitHub, so nothing checked them all.

`SURFACES` is that list. The test builds a two-installation session through the
real token preparation (`prepare_app_access`, `finish_app_delivery`,
`add_app_credentials`) with GitHub's token API faked, then resolves every
granted repo through every surface and checks the token against the one GitHub
minted for that repo's installation. It fails when a surface hands out another
installation's token, and when `add_app_credentials` writes a GitHub
credential no surface on the list accounts for: a new way to reach GitHub has to
be added here, with its check, before it ships.

The second test ties the agent guidance's claims about the GitHub MCP to the
tests that back them.
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.vaults.beta_managed_agents_environment_variable_auth_response import (
    BetaManagedAgentsEnvironmentVariableAuthResponse,
)
from anthropic.types.beta.vaults.beta_managed_agents_static_bearer_auth_response import (
    BetaManagedAgentsStaticBearerAuthResponse,
)
from cryptography.fernet import Fernet, MultiFernet
from daimon.core import github_app_session
from daimon.core._models import AgentGitHubGrant, Tenant, TenantGitHubRepo
from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK
from daimon.core.config import GithubAppSettings
from daimon.core.credential_env import render_github_repo_map
from daimon.core.github_app_session import (
    AppSessionAccess,
    add_app_credentials,
    finish_app_delivery,
    prepare_app_access,
    repo_credential_entries,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_requester_access import PermissionCache
from daimon.core.github_router_tokens import (
    TokenChoice,
    choose_token,
    load_grant_view,
    load_usable_rows,
    resolve_call,
)
from daimon.core.mcp_server_url import GITHUB_COPILOT_MCP_URL, github_router_url
from daimon.core.session_snapshot import session_mcp_servers
from daimon.core.stores import github_app_installations, github_router_sessions
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.testing.ma_models import ma_agent
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PUBLIC_URL = "https://daimon.example.com/mcp"
_WORKING_REPO = "acme/app"

type GrantedRepo = tuple[int, int, str, bool, Literal["read", "write"]]

#: (installation id, repo id, full name, working, access). Two organizations,
#: one App installation each; the working repo is in the first.
_TWO_ORGANIZATIONS: tuple[GrantedRepo, ...] = (
    (771, 101, "acme/app", True, "write"),
    (771, 102, "acme/infra", False, "write"),
    (772, 201, "client-org/product", False, "write"),
)
#: One organization whose grants need two tokens: a write and a read chunk.
_ONE_ORGANIZATION_TWO_CHUNKS: tuple[GrantedRepo, ...] = (
    (771, 101, "acme/app", True, "write"),
    (771, 103, "acme/docs", False, "read"),
)


# --- The session's GitHub credentials, as `add_app_credentials` leaves them ---


@dataclass(frozen=True)
class SessionCredentials:
    """What the sandbox and the session's MCP servers can present to GitHub."""

    #: Environment credentials: secret name -> value.
    env: Mapping[str, str]
    #: `static_bearer` credentials: MCP server URL -> token.
    bearers: Mapping[str, str]
    #: `github-repos.tsv`, as uploaded for `github-repo.sh`.
    repo_map: bytes
    access: AppSessionAccess


class _FakeVault:
    """The few vault calls `add_app_credentials` makes, kept in memory."""

    def __init__(self) -> None:
        self.env: dict[str, str] = {}
        self.bearers: dict[str, str] = {}

    async def list_credentials(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        for name in list(self.env):
            yield SimpleNamespace(id=f"env:{name}", auth=_env_auth(name))
        for url in list(self.bearers):
            yield SimpleNamespace(id=f"bearer:{url}", auth=_bearer_auth(url))

    async def store_credential(
        self, _client: Any, _vault_id: str, payload: Mapping[str, Any], **_kwargs: Any
    ) -> None:
        auth = payload["auth"]
        if auth["type"] == "environment_variable":
            self.env[auth["secret_name"]] = auth["secret_value"]
        else:
            self.bearers[auth["mcp_server_url"]] = auth["token"]

    async def update_credential(
        self,
        _client: Any,
        _vault_id: str,
        credential_id: str,
        payload: Mapping[str, Any],
        **_kwargs: Any,
    ) -> None:
        kind, key = credential_id.split(":", 1)
        auth = payload["auth"]
        if kind == "env":
            self.env[key] = auth["secret_value"]
        else:
            self.bearers[key] = auth["token"]

    async def delete_credential(
        self, _client: Any, _vault_id: str, credential_id: str, **_kwargs: Any
    ) -> None:
        kind, key = credential_id.split(":", 1)
        (self.env if kind == "env" else self.bearers).pop(key, None)

    async def add_github_copilot_credential(
        self, _client: Any, *, token: str, **_kwargs: Any
    ) -> None:
        self.bearers[GITHUB_COPILOT_MCP_URL] = token


def _env_auth(name: str) -> BetaManagedAgentsEnvironmentVariableAuthResponse:
    return BetaManagedAgentsEnvironmentVariableAuthResponse.model_validate(
        {
            "type": "environment_variable",
            "secret_name": name,
            "networking": {
                "type": "limited",
                "allowed_hosts": ["api.github.com", "github.com", "uploads.github.com"],
            },
            "injection_location": {"header": True, "body": False},
        }
    )


def _bearer_auth(url: str) -> BetaManagedAgentsStaticBearerAuthResponse:
    return BetaManagedAgentsStaticBearerAuthResponse.model_validate(
        {"type": "static_bearer", "mcp_server_url": url}
    )


# --- Surfaces -----------------------------------------------------------------


@dataclass(frozen=True)
class Surface:
    """One way an agent reaches GitHub.

    `credentials` are the names (env) or URLs (bearer) of the session
    credentials it presents. `covers` says which granted repos the agent
    guidance sends through it. `token_for` is the token it presents for a repo.
    """

    name: str
    credentials: frozenset[str]
    covers: Callable[[str], bool]
    token_for: Callable[[SessionCredentials, str], str | None]


def _is_working_repo(full_name: str) -> bool:
    # The guidance: "Plain `gh` and `GH_TOKEN` cover the working repo."
    return full_name == _WORKING_REPO


def _gh_token(credentials: SessionCredentials, _repo: str) -> str | None:
    # `gh` reads GH_TOKEN; `git clone`/`git push` put it in the URL.
    return credentials.env.get("GH_TOKEN")


def _helper_token(credentials: SessionCredentials, repo: str) -> str | None:
    # What github-repo.sh does: look the repo up in github-repos.tsv, use that variable.
    for line in credentials.repo_map.decode().splitlines():
        name, variable = line.split("\t")
        if name.lower() == repo.lower():
            return credentials.env.get(variable)
    return None


def _resolved_by_router(_credentials: SessionCredentials, _repo: str) -> str | None:
    return None  # per call, from the database: see `_router_token`


def _fake_jwt(*_args: object, **_kwargs: object) -> str:
    return "jwt"


def _direct_mcp_token(credentials: SessionCredentials, _repo: str) -> str | None:
    return credentials.bearers.get(GITHUB_COPILOT_MCP_URL)


_INSTALLATION_ENV = re.compile(r"GH_TOKEN_I[0-9]+_[0-9]+")


#: Every way a session reaches GitHub. Add a surface here, with how it picks its
#: token, before a new GitHub credential or route ships.
SURFACES: tuple[Surface, ...] = (
    # Resolved by the router per call (`_router_token`); its vault credential is
    # the router bearer, which is not a GitHub token.
    Surface(
        "github MCP via the router",
        frozenset({github_router_url(_PUBLIC_URL)}),
        lambda _repo: True,
        _resolved_by_router,
    ),
    Surface("gh with GH_TOKEN", frozenset({"GH_TOKEN"}), _is_working_repo, _gh_token),
    Surface(
        "git clone over https with GH_TOKEN", frozenset({"GH_TOKEN"}), _is_working_repo, _gh_token
    ),
    Surface("git push with GH_TOKEN", frozenset({"GH_TOKEN"}), _is_working_repo, _gh_token),
    Surface(
        "github-repo.sh (clone, gh, push)",
        frozenset({"GH_TOKEN_I*"}),
        lambda _repo: True,
        _helper_token,
    ),
    # A one-token session's `github` server uses this bearer. A routed session's
    # server points at the router instead (checked below), so it is never used there.
    Surface(
        "github MCP direct (one-token sessions)",
        frozenset({GITHUB_COPILOT_MCP_URL}),
        _is_working_repo,
        _direct_mcp_token,
    ),
)


def _surface_for_credential(name: str) -> Surface | None:
    key = "GH_TOKEN_I*" if _INSTALLATION_ENV.fullmatch(name) else name
    return next((surface for surface in SURFACES if key in surface.credentials), None)


# --- Session setup ------------------------------------------------------------


async def _seed_grants(
    factory: async_sessionmaker[AsyncSession], repos: tuple[GrantedRepo, ...]
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for installation_id, repo_id, name, _, _ in repos:
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
        logins = {installation_id: name.split("/")[0] for installation_id, _, name, _, _ in repos}
        for installation_id, login in logins.items():
            await github_app_installations.upsert_github_app(
                session,
                installation_id=installation_id,
                account_id=installation_id,
                account_login=login,
                account_type="Organization",
                repository_selection="selected",
                suspended_at=None,
            )
        for _, repo_id, _, working, access in repos:
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


@dataclass
class _FakeGitHub:
    """GitHub's installation-token endpoint: one distinct token per mint."""

    minted: dict[str, tuple[int, frozenset[int]]]

    def handler(self, request: httpx.Request) -> httpx.Response:
        match = re.fullmatch(r"/app/installations/([0-9]+)/access_tokens", request.url.path)
        assert match is not None, request.url.path
        installation_id = int(match.group(1))
        repo_ids = frozenset[int](json.loads(request.content)["repository_ids"])
        token = f"ghs_i{installation_id}_{len(self.minted)}"
        self.minted[token] = (installation_id, repo_ids)
        return httpx.Response(201, json={"token": token})

    def token_for_repo(self, repo_id: int) -> str:
        (token,) = [token for token, (_, ids) in self.minted.items() if repo_id in ids]
        return token


async def _routed_session(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fernet: MultiFernet,
    repos: tuple[GrantedRepo, ...],
) -> tuple[SessionCredentials, _FakeGitHub, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = await _seed_grants(factory, repos)
    async with factory.begin() as session:
        record, _bearer = await github_router_sessions.create(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)
    vault = _FakeVault()
    for name in (
        "list_credentials",
        "store_credential",
        "update_credential",
        "delete_credential",
        "add_github_copilot_credential",
    ):
        monkeypatch.setattr(github_app_session, name, getattr(vault, name))
    github = _FakeGitHub(minted={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(github.handler)) as client:
        access = await prepare_app_access(
            factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="pending:surfaces",
            config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
            fernet=fernet,
            cache=PermissionCache(),
            router_session_id=record.id,
        )
        await add_app_credentials(
            cast(AsyncAnthropic, SimpleNamespace()), vault_id="vlt_session", access=access
        )
        await finish_app_delivery(
            factory,
            client,
            access=access,
            provisional_session_id="pending:surfaces",
            session_id="sesn_surfaces",
        )
    credentials = SessionCredentials(
        env=dict(vault.env),
        bearers=dict(vault.bearers),
        repo_map=render_github_repo_map(repo_credential_entries(access)),
        access=access,
    )
    return credentials, github, record.id, tenant_id, agent_id


async def _router_token(
    factory: async_sessionmaker[AsyncSession],
    *,
    fernet: MultiFernet,
    router_session_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo: str,
) -> str | None:
    owner, name = repo.split("/", 1)
    async with factory() as session:
        rows = await load_usable_rows(session, router_session_id, fernet=fernet)
        grants = await load_grant_view(session, tenant_id=tenant_id, agent_id=agent_id)
    decision = choose_token(
        resolve_call("get_file_contents", {"owner": owner, "repo": name, "path": "README.md"}),
        rows,
        grants,
        grants.working_repo_id,
    )
    return decision.token.token if isinstance(decision, TokenChoice) else None


# --- Tests ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "repos",
    [
        pytest.param(_TWO_ORGANIZATIONS, id="two-organizations"),
        pytest.param(_ONE_ORGANIZATION_TWO_CHUNKS, id="one-organization-two-chunks"),
    ],
)
async def test_every_surface_uses_the_repo_installation_token(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    repos: tuple[GrantedRepo, ...],
) -> None:
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    credentials, github, router_id, tenant_id, agent_id = await _routed_session(
        db_session_factory, monkeypatch, fernet, repos
    )
    assert len(github.minted) > 1, "the session needs more than one token"

    # Every GitHub credential the session holds belongs to a listed surface.
    written = [*credentials.env, *credentials.bearers]
    unlisted = [name for name in written if _surface_for_credential(name) is None]
    assert not unlisted, (
        f"add_app_credentials wrote GitHub credentials no surface accounts for: {unlisted}. "
        "Add the new way of reaching GitHub to SURFACES with its check."
    )

    # A routed session's `github` server points at the router, never at the direct bearer.
    agent = ma_agent(
        id="ag_surfaces",
        mcp_servers=[{"name": "github", "type": "url", "url": GITHUB_COPILOT_MCP_URL}],
    )
    (server,) = session_mcp_servers(
        agent,
        frozenset(),
        tool_safety=OPEN_TOOL_SAFETY,
        public_url=_PUBLIC_URL,
        github_transport="router",
    )
    assert server.url == github_router_url(_PUBLIC_URL)

    by_name = {name: repo_id for _, repo_id, name, _, _ in repos}
    every_repo_reached: dict[str, set[str]] = {name: set() for name in by_name}
    for surface in SURFACES:
        for repo, repo_id in by_name.items():
            if not surface.covers(repo):
                continue
            if surface.name == "github MCP via the router":
                token = await _router_token(
                    db_session_factory,
                    fernet=fernet,
                    router_session_id=router_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo=repo,
                )
            else:
                token = surface.token_for(credentials, repo)
            assert token == github.token_for_repo(repo_id), (
                f"{surface.name} reaches {repo} with "
                f"{'no token' if token is None else 'the wrong token'}"
            )
            every_repo_reached[repo].add(surface.name)
    assert all(
        {"github MCP via the router", "github-repo.sh (clone, gh, push)"} <= reached
        for reached in every_repo_reached.values()
    ), every_repo_reached


def test_every_surface_name_is_unique() -> None:
    names = [surface.name for surface in SURFACES]
    assert len(names) == len(set(names))


# --- Guidance claims ------------------------------------------------------------

_TESTS = Path(__file__).resolve().parent

#: Each sentence of the agent guidance that tells the agent what the GitHub MCP
#: can read, or what a 404 means, mapped to the test that backs it. A new claim
#: fails `test_every_github_mcp_claim_names_a_backing_test` until it is listed.
_SURFACES_TEST = (
    "packages/core/tests/test_github_access_surfaces.py"
    "::test_every_surface_uses_the_repo_installation_token"
)
GITHUB_MCP_CLAIMS: Mapping[str, str] = {
    "The GitHub MCP is still fine for reading.": _SURFACES_TEST,
    # The helper paragraph: which repos go through github-repo.sh, and which
    # through plain gh and GH_TOKEN (the surfaces' `covers`).
    "your repos need more than one GitHub token": _SURFACES_TEST,
    "For any repo other than the working repo, use the helper": _SURFACES_TEST,
    "Plain `gh` and `GH_TOKEN` cover the working repo.": _SURFACES_TEST,
}


def _sentences(text: str) -> list[str]:
    flat = " ".join(text.split())
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", flat) if part.strip()]


def _is_github_mcp_claim(sentence: str) -> bool:
    """A claim about which GitHub surface reaches which repo, or what a 404 means."""
    lowered = sentence.lower()
    return (
        "404" in lowered
        or ("github mcp" in lowered and "read" in lowered)
        or "github-repo.sh exists" in lowered
        or "other than the working repo, use the helper" in lowered
        or "cover the working repo" in lowered
    )


def _test_exists(node_id: str) -> bool:
    path, _, name = node_id.partition("::")
    file = _TESTS.parents[2] / path
    if not file.is_file():
        return False
    tree = ast.parse(file.read_text())
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
        for node in tree.body
    )


def test_every_github_mcp_claim_names_a_backing_test() -> None:
    claims = [s for s in _sentences(CREDENTIAL_GUIDANCE_BLOCK) if _is_github_mcp_claim(s)]
    assert claims, "the guidance makes no GitHub MCP claim; drop this test with the last one"
    unmapped = [claim for claim in claims if not any(key in claim for key in GITHUB_MCP_CLAIMS)]
    assert not unmapped, f"map these guidance claims to the test that backs them: {unmapped}"
    for key, node_id in GITHUB_MCP_CLAIMS.items():
        assert any(key in claim for claim in claims), f"stale claim key: {key!r}"
        assert _test_exists(node_id), f"{node_id} backs {key!r} but does not exist"

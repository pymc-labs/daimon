"""The agent route tests' shared setup: an owned agent, people, a signed-in client."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import Fernet
from daimon.adapters.web.app import create_app
from daimon.adapters.web.cookies import new_session_token, token_sha256
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.turns import WebTurns
from daimon.core.authz import build_subject
from daimon.core.channel_backend import NO_CAPTURE, BackendActor, make_backend_service
from daimon.core.config import Settings, TurnSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import WebPersonRow
from daimon.core.turn.deps import build_turn_deps
from daimon.testing.factories import make_account, make_tenant, make_web_person
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, json_body
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME, DEFAULT_ENV_NAME
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import PUBLIC_URL, web_settings

OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
NOW = datetime(2030, 1, 15, 12, tzinfo=UTC)


class Agents:
    """One agent in MA whose version moves with each update."""

    def __init__(self, agent: BetaManagedAgentsAgent) -> None:
        self.agent = agent
        self.updates: list[dict[str, Any]] = []
        self.router = MARouter()
        self.router.add("GET", r"/v1/agents$", lambda _r, _m: self._list())
        self.router.add("GET", r"/v1/agents/ag_w$", lambda _r, _m: self._json())
        self.router.add("POST", r"/v1/agents/ag_w$", self._update)
        self.skills: list[dict[str, Any]] = []
        self.router.add(
            "GET",
            r"/v1/skills$",
            lambda _r, _m: httpx.Response(
                200, json={"data": self.skills, "has_more": False, "next_page": None}
            ),
        )

    def _json(self) -> httpx.Response:
        return httpx.Response(200, json=self.agent.model_dump(mode="json"))

    def _list(self) -> httpx.Response:
        body = {"data": [self.agent.model_dump(mode="json")], "has_more": False}
        return httpx.Response(200, json=body)

    def _update(self, req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        self.updates.append(body)
        fields = {k: body[k] for k in ("description", "system") if k in body}
        self.agent = self.agent.model_copy(update={"version": self.agent.version + 1, **fields})
        return self._json()


async def agent_people(
    session: AsyncSession, **agent_fields: Any
) -> tuple[Agents, WebPersonRow, WebPersonRow]:
    """An agent owned by one member, and another member of the same workspace."""
    tenant = await make_tenant(session)
    owner = await make_web_person(
        session, tenant=tenant, account=await make_account(session, tenant=tenant)
    )
    other = await make_web_person(
        session,
        tenant=tenant,
        account=await make_account(session, tenant=tenant),
        display_name="Other",
    )
    await agent_owners.record_creator(
        session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_w"),
        account_id=owner.account_id,
    )
    await session.commit()
    agents = Agents(
        ma_agent(
            id="ag_w",
            name="helper",
            tenant_id=tenant.id,
            metadata={"daimon_account": str(owner.account_id)},
            system="Be brief.",
            description="Answers questions",
            version=3,
            updated_at=datetime(2030, 1, 1, tzinfo=UTC),
            **agent_fields,
        )
    )
    return agents, owner, other


async def sign_in_token(factory: async_sessionmaker[AsyncSession], person: WebPersonRow) -> str:
    token = new_session_token()
    async with factory() as db, db.begin():
        await identities_store.link_identity(
            db,
            tenant_id=person.tenant_id,
            person_id=person.id,
            provider="discord",
            issuer="",
            subject=str(person.id),
            label="person@example.com",
            groups=(),
        )
        await sessions_store.create_session(
            db,
            token_sha256=token_sha256(token),
            provider="discord",
            issuer="",
            subject=str(person.id),
            people=[person],
            ttl=NOW - datetime(2029, 1, 1, tzinfo=UTC),
            now=NOW,
        )
    return token


@asynccontextmanager
async def agent_client(
    agents: Agents,
    tenant_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    token: str,
    **extra: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    db = {"url": "postgresql+asyncpg://u:p@h/db"}
    settings = extra.pop("settings", None) or Settings(_env_file=None, database=db)  # pyright: ignore[reportCallIssue, reportArgumentType]
    web = extra.pop("web", None) or web_settings()
    anthropic = build_fake_anthropic(agents.router.dispatch)
    if extra.pop("with_turns", False):
        # A switch reads the agent's source through the turn machinery.
        environment = ma_environment(tenant_id=tenant_id)
        agents.router.add_environment_list(environment)
        agents.router.add_environment(environment)
        extra["turns"] = WebTurns(
            settings,
            web,
            build_turn_deps(
                settings,
                anthropic,
                factory,
                deployment_default=DeploymentDefault(
                    agent_name=DEFAULT_AGENT_NAME, environment_name=DEFAULT_ENV_NAME
                ),
                resolver_cache=new_resolver_cache(),
                billing_config=None,
            ),
        )
    async with httpx.AsyncClient() as http:
        runtime = WebRuntime(
            settings=settings,
            web=web,
            sessionmaker=factory,
            http_client=http,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            anthropic=anthropic,
            now=lambda: NOW,
            **extra,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)), base_url=PUBLIC_URL
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            yield client


async def make_admin(factory: async_sessionmaker[AsyncSession], person: WebPersonRow) -> None:
    async with factory.begin() as db:
        await roles_store.set_workspace_role(
            db,
            tenant_id=person.tenant_id,
            account_id=person.account_id,
            role="owner",
            granted_by_account_id=None,
        )


async def provider_choice(
    factory: async_sessionmaker[AsyncSession], owner: WebPersonRow, providers: tuple[str, ...]
) -> Settings:
    """Settings with provider choice on, and ``providers`` allowed in the owner's workspace."""
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        turn=TurnSettings(path="mux", channel_backends=True),
    )
    service = make_backend_service(factory, settings.turn, capture_source=NO_CAPTURE)
    policy = await service.tenant_policy(owner.tenant_id)
    await service.set_tenant_allowlist(
        owner.tenant_id,
        providers,  # pyright: ignore[reportArgumentType]
        policy.revision,
        BackendActor(
            tenant_id=owner.tenant_id,
            account_id=owner.account_id,
            subject=build_subject(is_admin=True, platform_user_id=None),
        ),
    )
    return settings


async def consent_body(
    client: httpx.AsyncClient, value: str = "openai:gpt-6-astra"
) -> dict[str, Any]:
    """The apply body that accepts what the model preview says the switch drops."""
    preview = (await client.get(f"/api/agents/ag_w/model-preview?value={value}")).json()
    return {
        "accepted_drops": preview["drops"],
        "preview_digest": preview["digest"],
        "preview_revision": preview["revision"],
    }

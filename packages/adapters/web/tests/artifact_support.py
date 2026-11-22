"""A workspace with people, a page published through the core store, and a client."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet
from daimon.adapters.web.app import create_app
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.artifact_pages.access import PageChange
from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores import artifact_pages as artifact_store
from daimon.core.stores import web_people
from daimon.core.stores.domain import ArtifactRow, WebPersonRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.turn_router import AGENT_ID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import funded_workspace, signed_in, visitor_of
from .conftest import PUBLIC_URL, web_settings

KEYS = (Fernet.generate_key().decode(),)
HTML = "<!doctype html><html><head><title>Poll</title></head><body>Vote</body></html>"
COOKIE = "__Host-daimon_web"


def page_settings() -> Settings:
    """Artifacts need a public address and crypto keys to be on."""
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        web={"public_url": PUBLIC_URL},  # pyright: ignore[reportArgumentType]
        crypto={"keys": list(KEYS)},  # pyright: ignore[reportArgumentType]
    )


@dataclass(frozen=True)
class Team:
    tenant_id: uuid.UUID
    owner: WebPersonRow
    member: WebPersonRow
    visitor: WebPersonRow


async def member_of(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, name: str
) -> WebPersonRow:
    person_id = uuid.uuid4()
    async with factory.begin() as db:
        principal = await get_or_create_platform_principal(
            db, tenant_id=tenant_id, platform="web", external_id=str(person_id)
        )
        return await web_people.upsert_member(
            db,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            display_name=name,
            workspace_name="Acme",
            email=f"{name.lower()}@example.com",
            new_person_id=person_id,
        )


async def team(factory: async_sessionmaker[AsyncSession]) -> Team:
    ws = await funded_workspace(factory)
    member = await member_of(factory, ws.tenant_id, "Bea")
    return Team(ws.tenant_id, ws.person, member, await visitor_of(factory, ws))


async def publish(
    factory: async_sessionmaker[AsyncSession],
    owner: WebPersonRow,
    *,
    slug: str = "poll",
    html: str = HTML,
    **change: Any,
) -> ArtifactRow:
    async with factory.begin() as db:
        return await artifact_store.publish(
            db,
            tenant_id=owner.tenant_id,
            agent_id=AGENT_ID,
            account_id=owner.account_id,
            is_admin=False,
            slug=slug,
            title=slug.title(),
            html=html,
            change=PageChange(**change),
            origin=None,
            new_link=False,
            now=datetime.now(UTC),
        )


@asynccontextmanager
async def page_client(
    factory: async_sessionmaker[AsyncSession], person: WebPersonRow | None = None, **extra: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """The app signed in as `person` (anonymous without one)."""
    settings = extra.pop("settings", None) or page_settings()
    async with httpx.AsyncClient() as http:
        runtime = WebRuntime(
            settings=settings,
            web=web_settings(),
            sessionmaker=factory,
            http_client=http,
            fernet=build_multifernet(KEYS),
            **extra,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)), base_url=PUBLIC_URL
        ) as client:
            if person is not None:
                token = await signed_in(factory, person)
                client.cookies.set(COOKIE, token, domain="web.example.com")
            yield client

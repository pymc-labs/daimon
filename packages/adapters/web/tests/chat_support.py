"""A funded workspace, a signed-up person and real turns over a scripted Managed Agents."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from daimon.adapters.web.cookies import new_session_token, token_sha256
from daimon.adapters.web.invites import issue_link
from daimon.adapters.web.turns import WebTurns
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import web_audit, web_identities, web_invites, web_people, web_sessions
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import WebMessageBlocks, WebMessageRow, WebPersonRow, WebThreadRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.turn.deps import build_turn_deps
from daimon.testing.turn_router import AGENT_ID, ENV_ID
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from mux.testing.drivers.anthropic.ma_models import DEFAULT_AGENT_NAME, DEFAULT_ENV_NAME, ma_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import web_settings

SESSION_ID = "sesn_web"
STREAM = rf"/v1/sessions/{SESSION_ID}/events/stream"
TURN_DEFAULT = DeploymentDefault(agent_name=DEFAULT_AGENT_NAME, environment_name=DEFAULT_ENV_NAME)


def test_settings() -> Settings:
    return Settings(_env_file=None, database={"url": "postgresql+asyncpg://u:p@h/db"})  # pyright: ignore[reportCallIssue, reportArgumentType]


@dataclass(frozen=True)
class Workspace:
    tenant_id: uuid.UUID
    person: WebPersonRow


async def funded_workspace(factory: async_sessionmaker[AsyncSession]) -> Workspace:
    """A provisioned workspace with credit and one member signed up on the web."""
    tenant = await provision_tenant(
        factory,
        platform="discord",
        workspace_id=f"g-{uuid.uuid4().hex[:8]}",
        signup_credit=Decimal("100"),
    )
    person_id = uuid.uuid4()
    async with factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant.tenant_id, platform="web", external_id=str(person_id)
        )
        person = await web_people.upsert_member(
            session,
            tenant_id=tenant.tenant_id,
            account_id=principal.account_id,
            display_name="Ada",
            workspace_name="Acme",
            email="ada@example.com",
            new_person_id=person_id,
        )
    return Workspace(tenant.tenant_id, person)


async def visitor_of(factory: async_sessionmaker[AsyncSession], ws: Workspace) -> WebPersonRow:
    """A visitor from a fresh invite to the workspace, on an account of their own."""
    person_id = uuid.uuid4()
    async with factory.begin() as session:
        invite, _ = await issue_link(
            session,
            tenant_id=ws.tenant_id,
            kind="invite",
            expires_at=datetime.now(UTC) + timedelta(days=7),
            created_by_account_id=ws.person.account_id,
        )
        principal = await get_or_create_platform_principal(
            session, tenant_id=ws.tenant_id, platform="web", external_id=str(person_id)
        )
        return await web_people.create_invited(
            session,
            person_id=person_id,
            tenant_id=ws.tenant_id,
            account_id=principal.account_id,
            kind="visitor",
            display_name="Guest",
            workspace_name="Acme",
            email=None,
            invite_id=invite.id,
            invited_by_account_id=ws.person.account_id,
        )


def build_turns(
    factory: async_sessionmaker[AsyncSession], router: MARouter, defaults_root: Path
) -> WebTurns:
    settings = test_settings()
    settings.defaults_root = defaults_root
    deps = build_turn_deps(
        settings,
        build_fake_anthropic(router.dispatch),
        factory,
        deployment_default=TURN_DEFAULT,
        resolver_cache=new_resolver_cache(),
        billing_config=None,
    )
    return WebTurns(settings, web_settings(render_interval_s=0.01), deps)


@contextmanager
def one_session() -> Iterator[None]:
    """Every turn binds `SESSION_ID` instead of creating a session."""
    session = ma_session(id=SESSION_ID, agent_id=AGENT_ID, environment_id=ENV_ID)
    with patch("daimon.core.turn.prepare.create_session", return_value=session):
        yield


async def new_thread(
    factory: async_sessionmaker[AsyncSession], ws: Workspace, text: str = "hi"
) -> tuple[WebThreadRow, WebMessageRow]:
    async with factory.begin() as session:
        thread = await chat_store.create_thread(
            session,
            tenant_id=ws.tenant_id,
            person_id=ws.person.id,
            agent_id=AGENT_ID,
            agent_name=DEFAULT_AGENT_NAME,
            title="New chat",
        )
        message, _ = await chat_store.insert_user_message(
            session,
            tenant_id=ws.tenant_id,
            thread_id=thread.id,
            message_id=uuid.uuid4(),
            content=text,
            blocks=WebMessageBlocks(),
        )
    return thread, message


async def signed_in(factory: async_sessionmaker[AsyncSession], person: WebPersonRow) -> str:
    """A live sign-in for `person`; its session cookie value."""
    token = new_session_token()
    async with factory.begin() as db:
        await web_identities.link_identity(
            db,
            tenant_id=person.tenant_id,
            person_id=person.id,
            provider="discord",
            issuer="",
            subject=str(person.id),
            label=person.email or "",
            groups=(),
        )
        await web_sessions.create_session(
            db,
            token_sha256=token_sha256(token),
            provider="discord",
            issuer="",
            subject=str(person.id),
            people=[person],
            ttl=timedelta(days=1),
            now=datetime.now(UTC),
        )
    return token


def sse_frames(body: str) -> list[tuple[str, dict[str, Any]]]:
    """The `(id, event)` frames of an event-stream body."""
    frames: list[tuple[str, dict[str, Any]]] = []
    for chunk in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines() if ": " in line)
        if "data" in lines:
            frames.append((lines["id"], json.loads(lines["data"])))
    return frames


async def shown_trail(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[tuple[str, str, str | None]]:
    """The audit trail oldest first as `(action, target, detail)`, a person or invite
    shown by name or address as the admin page shows them."""
    async with factory() as db:
        rows = await web_audit.list_recent(db, tenant_id=tenant_id)
        people = await web_people.list_people(db, tenant_id=tenant_id)
        emails = await web_invites.emails_by_id(
            db,
            tenant_id=tenant_id,
            invite_ids=[r.target_invite_id for r in rows if r.target_invite_id],
        )
    names = {p.id: p.display_name for p in people}
    return [
        (
            r.action,
            (names.get(r.target_person_id) if r.target_person_id else None)
            or (emails.get(r.target_invite_id) if r.target_invite_id else None)
            or r.target,
            r.detail,
        )
        for r in reversed(rows)
    ]

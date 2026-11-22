"""Artifact tools: publish from an upload, who may call what, and page-event watches."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import _session_gate
from daimon.adapters.mcp.tools import watches as watch_tools
from daimon.adapters.mcp.tools.artifacts import (
    list_impl,
    publish_impl,
    query_impl,
    upload_url_impl,
    write_impl,
)
from daimon.core.artifact_pages.access import PageChange
from daimon.core.artifact_pages.keys import page_event_key
from daimon.core.continuity.page_events import sign_page_event
from daimon.core.membership import Membership
from daimon.core.stores import artifact_pages as store
from daimon.core.stores import watches, web_people
from daimon.core.stores.domain import AccountRow, Role, TenantRow
from daimon.core.stores.file_uploads import store_upload_content
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import bind_origin_session
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from daimon.testing.watch_fixtures import install_poll_ports
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import auth_identity, discord_turn_origin

pytestmark = pytest.mark.fresh_schema

PUBLIC_URL = "https://daimon.example.com"
CRYPTO_KEY = Fernet.generate_key().decode()
HTML = "<!doctype html><html><head></head><body>Votes</body></html>"


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    runtime: SimpleNamespace
    tenant: TenantRow
    account: AccountRow
    person_id: uuid.UUID
    auth: AuthIdentity


@pytest.fixture
async def world(db_session_factory: async_sessionmaker[AsyncSession], monkeypatch) -> World:
    """A workspace member who is also a Discord user with a live thread, and a runtime
    with a page host and one crypto key."""
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    async with db_session_factory.begin() as db:
        await install_poll_ports(db)
        await watches.set_watch_control(db, scope="global", enabled=True)
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        await make_platform_principal(
            db, platform="discord", external_id="123", tenant=tenant, account=account
        )
        person = await make_web_person(db, tenant=tenant, account=account, display_name="Ada")
        await create_thread_session(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="4242",
            channel_id="4241",
            account_id=account.id,
            ma_session_id="session-bound",
            ma_agent_id="agent-bound",
        )
    runtime = SimpleNamespace(
        session_factory=db_session_factory,
        client=object(),
        membership_resolver=SimpleNamespace(
            resolve=AsyncMock(return_value=Membership(is_member=True))
        ),
        settings=SimpleNamespace(
            web=SimpleNamespace(public_url=PUBLIC_URL),
            crypto=SimpleNamespace(keys=(SecretStr(CRYPTO_KEY),)),
            mcp=SimpleNamespace(
                app_root_url="https://os.example", public_url="https://os.example/mcp"
            ),
            direct_message_policies={},
        ),
    )
    auth = auth_identity(
        account.id,
        tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="123",
        external_id=tenant.external_id,
        chat_session_id="grant",
    )
    return World(db_session_factory, runtime, tenant, account, person.id, auth)


@asynccontextmanager
async def turn(world: World, *, human: bool = True) -> AsyncIterator[str]:
    """A live Discord turn of the member; `human` says whether their own message started it
    (a routine's turn is not)."""
    async with discord_turn_origin(
        world.factory,
        world.tenant,
        world.account,
        parent_channel_id="4241",
        thread_id="4242",
        responder_ma_agent_id="agent-bound",
        chat_session_id="grant",
        platform_message_id="m-1",
    ) as origin:
        async with world.factory.begin() as db:
            await bind_origin_session(
                db, origin_id=origin.id, chat_session_id="grant", human_invocation=human
            )
        yield str(origin.id)


async def put_page(world: World, slug: str = "votes", **change) -> None:
    """Publish `slug` for the member straight through the store."""
    async with world.factory.begin() as db:
        await store.publish(
            db,
            tenant_id=world.tenant.id,
            agent_id="agent-bound",
            account_id=world.account.id,
            is_admin=False,
            slug=slug,
            title="Votes",
            html=HTML,
            change=PageChange(**change),
            origin=None,
            new_link=False,
            now=datetime.now(UTC),
        )


async def test_publish_from_an_upload_makes_a_version_at_the_page_address(world: World) -> None:
    """The upload handle's bytes become version 1, the result is the page address, and the
    version records the message whose turn published it."""
    slot = await upload_url_impl(world.runtime, world.auth, "votes.html")
    token = slot.upload_url.rsplit("/", 1)[1]
    async with world.factory.begin() as db:
        landed = await store_upload_content(
            db, upload_token=token, data=HTML.encode(), now=datetime.now(UTC)
        )
    assert landed is not None, "the PUT landed"

    async with turn(world) as origin_id:
        page = await publish_impl(
            world.runtime,
            world.auth,
            origin_context_id=origin_id,
            slug="votes",
            upload=slot.upload,
            title="Votes",
            events=True,
        )

    assert page.version == 1 and page.events, page
    async with world.factory() as db:
        artifact = await store.get_by_slug(db, tenant_id=world.tenant.id, slug="votes")
        assert artifact is not None
        version = await store.get_version(db, artifact.id, 1)
    assert page.url == f"{PUBLIC_URL}/a/{artifact.public_id}/votes"
    assert version is not None and version.html == HTML
    assert (version.origin_platform, version.origin_thread_id, version.origin_message_id) == (
        "discord",
        "4242",
        "m-1",
    )
    assert page.owner is not None and page.owner.id == world.person_id


async def test_a_routine_may_query_and_write_but_not_publish(world: World) -> None:
    """A turn no person's message started is refused publishing; list, query and write
    need no origin and work."""
    await put_page(world)
    async with turn(world, human=False) as origin_id:
        with pytest.raises(ToolError, match="Only a person in this conversation"):
            await publish_impl(
                world.runtime,
                world.auth,
                origin_context_id=origin_id,
                slug="votes",
                title="Renamed",
            )

    written = await write_impl(
        world.runtime,
        world.auth,
        slug="votes",
        collection="votes",
        doc_id="a",
        data={"choice": "yes"},
        mode="set",
    )
    assert written.revision == 1 and not written.deleted
    seen = await query_impl(
        world.runtime,
        world.auth,
        slug="votes",
        collection="votes",
        order_by=None,
        descending=False,
        limit=None,
    )
    assert "untrusted" in seen and json.dumps({"choice": "yes"}) in seen, seen
    assert [p.slug for p in (await list_impl(world.runtime, world.auth)).pages] == ["votes"]


async def test_a_caller_the_workspace_does_not_admit_is_refused(world: World) -> None:
    """A suspended person can use no page tool, and a member cannot use a page that is
    only for other people: it reads as missing."""
    await put_page(world, access="people")
    async with world.factory.begin() as db:
        stranger = await make_account(db, tenant=world.tenant)
        await make_web_person(db, tenant=world.tenant, account=stranger, email="b@example.com")
    other = auth_identity(stranger.id, world.tenant.id, role=Role.USER, platform="discord")
    with pytest.raises(ToolError, match="No page named 'votes'"):
        await query_impl(
            world.runtime,
            other,
            slug="votes",
            collection="votes",
            order_by=None,
            descending=False,
            limit=None,
        )

    async with world.factory.begin() as db:
        await web_people.update_person(db, person_id=world.person_id, status="suspended")
    with pytest.raises(ToolError, match="Only people in the workspace"):
        await list_impl(world.runtime, world.auth)


async def test_create_watch_on_a_page_enrolls_a_key_the_page_host_derives(
    world: World, monkeypatch
) -> None:
    """`create_watch(source="page_event")` binds the page and enrolls a key that the page
    host's derivation reproduces, so an event signed with it is accepted."""
    await put_page(world, events=True)
    monkeypatch.setattr(
        _session_gate,
        "retrieve_session",
        AsyncMock(
            return_value=ma_session(
                agent=ma_session_agent(
                    id="agent-bound",
                    tools=[
                        {
                            "type": "mcp_toolset",
                            "mcp_server_name": "daimon-mcp",
                            "default_config": {
                                "enabled": True,
                                "permission_policy": {"type": "always_allow"},
                            },
                            "configs": [
                                {
                                    "name": "create_watch",
                                    "enabled": True,
                                    "permission_policy": {"type": "always_ask"},
                                }
                            ],
                        }
                    ],
                )
            )
        ),
    )
    async with turn(world) as origin_id:
        made = await watch_tools.create_watch_impl(
            world.runtime,
            world.auth,
            origin_context_id=origin_id,
            instructions="Tally the votes.",
            source="page_event",
            page="votes",
        )
        with pytest.raises(ToolError, match="Name the page"):
            await watch_tools.create_watch_impl(
                world.runtime,
                world.auth,
                origin_context_id=origin_id,
                instructions="Tally the votes.",
                source="page_event",
            )

    assert made.source == "page_event" and made.key_id is not None
    now = datetime.now(UTC)
    async with world.factory.begin() as db:
        artifact = await store.get_by_slug(db, tenant_id=world.tenant.id, slug="votes")
        assert artifact is not None
        assert await watches.live_key_id(db, made.watch_id, now=now) == made.key_id
        key = page_event_key((CRYPTO_KEY,), made.watch_id, made.key_id)
        assert key is not None
        event = sign_page_event(
            key=key,
            watch_id=made.watch_id,
            page_id=artifact.id,
            key_id=made.key_id,
            body=b"{}",
            now=now,
        )
        assert await watches.accept_page_event(db, event, key=key) is not None

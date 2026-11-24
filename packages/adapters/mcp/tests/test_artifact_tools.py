"""Artifact tools: publish from an upload, who may call what, and page-event watches."""

from __future__ import annotations

import html
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
from daimon.adapters.mcp.tools import artifacts as artifact_tools
from daimon.adapters.mcp.tools import watches as watch_tools
from daimon.adapters.mcp.tools.artifacts import (
    PersonCall,
    Written,
    delete_impl,
    hub_call,
    list_impl,
    person_call,
    publish_impl,
    query_impl,
    source_impl,
    transfer_impl,
    upload_url_impl,
    write_impl,
)
from daimon.core.artifact_pages.access import PageChange
from daimon.core.artifact_pages.documents import CollectionRule, DocumentWrite
from daimon.core.artifact_pages.keys import page_event_key
from daimon.core.config import ArtifactPagesSettings, Settings
from daimon.core.continuity.page_events import sign_page_event
from daimon.core.membership import Membership
from daimon.core.stores import artifact_pages as store
from daimon.core.stores import watches, web_people
from daimon.core.stores.domain import AccountRow, Role, TenantRow
from daimon.core.stores.file_uploads import store_upload_content
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import bind_origin_session
from daimon.core.stores.workspace_members import upsert_member
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from daimon.testing.outsiders import INTERNAL_ROLE, hold_audience, link_discord_member
from daimon.testing.watch_fixtures import install_poll_ports
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import auth_identity, discord_turn_origin

pytestmark = pytest.mark.fresh_schema

PUBLIC_URL = "https://daimon.example.com"
CRYPTO_KEY = Fernet.generate_key().decode()
LIMITS = ArtifactPagesSettings()
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
        settings=Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
            web={"public_url": PUBLIC_URL},  # pyright: ignore[reportArgumentType]
            crypto={"keys": [CRYPTO_KEY]},  # pyright: ignore[reportArgumentType]
            mcp={"public_url": "https://os.example/mcp"},  # pyright: ignore[reportArgumentType]
            artifact_pages=LIMITS,
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
            limits=LIMITS,
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
            by=await person_call(world.runtime, world.auth, origin_id),
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
            await person_call(world.runtime, world.auth, origin_id)

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
    """A suspended person can use no page tool nor be named on a page, a member cannot use
    a page that is only for other people (it reads as missing), and only those who manage
    a page read its older versions."""
    await put_page(world, access="people")
    async with world.factory.begin() as db:
        stranger = await make_account(db, tenant=world.tenant)
        theirs = await make_web_person(
            db, tenant=world.tenant, account=stranger, email="b@example.com"
        )
        for html in (HTML, "<p>2</p>"):
            await store.publish(
                db,
                tenant_id=world.tenant.id,
                agent_id="agent-bound",
                account_id=stranger.id,
                is_admin=False,
                slug="notes",
                title="Notes",
                html=html,
                change=PageChange(),
                origin=None,
                new_link=False,
                now=datetime.now(UTC),
                limits=LIMITS,
            )
    async with turn(world) as origin_id:
        by = await person_call(world.runtime, world.auth, origin_id)
        current = await source_impl(world.runtime, world.auth, by=by, slug="notes", version=None)
        with pytest.raises(ToolError, match="older versions"):
            await source_impl(world.runtime, world.auth, by=by, slug="notes", version=1)
    assert current.version == 2
    assert not any(
        p.visible_outside_workspace for p in (await list_impl(world.runtime, world.auth)).pages
    )
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

    async with turn(world) as origin_id:
        async with world.factory.begin() as db:
            await web_people.update_person(db, person_id=theirs.id, status="suspended")
        with pytest.raises(ToolError, match="Not a person in the app"):
            await publish_impl(
                world.runtime,
                world.auth,
                by=await person_call(world.runtime, world.auth, origin_id),
                slug="votes",
                people=[str(theirs.id)],
            )

    async with world.factory.begin() as db:
        await web_people.update_person(db, person_id=world.person_id, status="suspended")
    with pytest.raises(ToolError, match="Only members can use pages"):
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


async def test_query_reads_one_document_with_who_changed_it_and_its_revision(world: World) -> None:
    """Each document carries `updated_by` and `revision`; `doc_id` reads just that one."""
    await put_page(world)
    for doc_id in ("a", "b"):
        await write_impl(
            world.runtime,
            world.auth,
            slug="votes",
            collection="votes",
            doc_id=doc_id,
            data={"choice": doc_id},
            mode="set",
        )
    seen = await query_impl(
        world.runtime,
        world.auth,
        slug="votes",
        collection="votes",
        order_by=None,
        descending=False,
        limit=None,
        doc_id="b",
    )
    body = json.loads(html.unescape(seen).splitlines()[-2])
    assert [(d["id"], d["updated_by"], d["revision"]) for d in body] == [("b", "agent", 2)], body


async def test_a_members_agent_follows_the_collection_rules_a_manager_is_free_of(
    world: World,
) -> None:
    """Tools act as their person: a member's agent reads only that member's documents of
    an `own` collection and cannot write another's, while the page's owner sees all."""
    await put_page(world, collection_rules={"votes": CollectionRule(read="own", write="own")})
    for doc_id in ("ada", "bob"):
        await write_impl(
            world.runtime,
            world.auth,
            slug="votes",
            collection="votes",
            doc_id=doc_id,
            data={"by": doc_id},
            mode="set",
        )
    async with world.factory.begin() as db:
        member = await make_account(db, tenant=world.tenant)
        await make_platform_principal(
            db, platform="discord", external_id="456", tenant=world.tenant, account=member
        )
        await store.write_doc(
            db,
            (await store.get_by_slug(db, tenant_id=world.tenant.id, slug="votes")),  # type: ignore[arg-type]
            "votes",
            "mine",
            DocumentWrite(data={"by": "member"}),
            by="member",
            account_id=member.id,
            rule=None,
            now=datetime.now(UTC),
            limits=LIMITS,
        )
    theirs = auth_identity(
        member.id,
        world.tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="456",
        external_id=world.tenant.external_id,
        chat_session_id="grant",
    )

    async def ids(auth: AuthIdentity, doc_id: str | None = None) -> list[str]:
        seen = await query_impl(
            world.runtime,
            auth,
            slug="votes",
            collection="votes",
            order_by=None,
            descending=False,
            limit=None,
            doc_id=doc_id,
        )
        return [d["id"] for d in json.loads(html.unescape(seen).splitlines()[-2])]

    assert sorted(await ids(world.auth)) == ["ada", "bob", "mine"], "a manager reads all"
    assert await ids(theirs) == ["mine"], "a member reads only their own"
    assert await ids(theirs, "ada") == [], "and not another's by id"
    with pytest.raises(ToolError, match="only their own"):
        await write_impl(
            world.runtime,
            theirs,
            slug="votes",
            collection="votes",
            doc_id="ada",
            data={"by": "member"},
            mode="set",
        )


async def test_a_members_agent_deletes_only_what_the_collection_rule_lets_it(
    world: World,
) -> None:
    """A delete is a write: under `write: own` a member's agent removes only that member's
    document, under `write: none` none, while the page's owner removes any."""
    await put_page(
        world,
        collection_rules={
            "own": CollectionRule(write="own"),
            "frozen": CollectionRule(write="none"),
        },
    )
    async with world.factory.begin() as db:
        member = await make_account(db, tenant=world.tenant)
        await make_platform_principal(
            db, platform="discord", external_id="456", tenant=world.tenant, account=member
        )
        page = await store.get_by_slug(db, tenant_id=world.tenant.id, slug="votes")
        assert page is not None
        for collection, doc_id, account in (
            ("own", "ada", world.account),
            ("own", "mine", member),
            ("frozen", "mine", member),
        ):
            await store.write_doc(
                db,
                page,
                collection,
                doc_id,
                DocumentWrite(data={"by": doc_id}),
                by="member",
                account_id=account.id,
                rule=None,
                now=datetime.now(UTC),
                limits=LIMITS,
            )
    theirs = auth_identity(
        member.id,
        world.tenant.id,
        role=Role.USER,
        platform="discord",
        platform_user_id="456",
        external_id=world.tenant.external_id,
        chat_session_id="grant",
    )

    async def delete(auth: AuthIdentity, collection: str, doc_id: str) -> Written:
        return await write_impl(
            world.runtime,
            auth,
            slug="votes",
            collection=collection,
            doc_id=doc_id,
            data=None,
            mode="set",
        )

    with pytest.raises(ToolError, match="only their own"):
        await delete(theirs, "own", "ada")
    with pytest.raises(ToolError, match="read-only"):
        await delete(theirs, "frozen", "mine")
    assert (await delete(theirs, "own", "mine")).deleted, "a member removes their own"
    assert (await delete(world.auth, "own", "ada")).deleted, "the owner removes any"
    assert (await delete(world.auth, "frozen", "mine")).deleted, "and under a read-only rule"


async def test_a_turn_an_outside_pages_event_woke_cannot_write_to_an_outside_page(
    world: World, monkeypatch
) -> None:
    """Only a turn no person started is checked; it may still write to a workspace page."""
    await put_page(world, slug="board")
    await put_page(world, access="public")
    monkeypatch.setattr(store, "woken_by_outside_page", AsyncMock(return_value=True))

    async def write(slug: str) -> object:
        return await write_impl(
            world.runtime,
            world.auth,
            slug=slug,
            collection="votes",
            doc_id="a",
            data={"n": 1},
            mode="set",
        )

    async with turn(world, human=False):
        with pytest.raises(ToolError, match="cannot write to such a page"):
            await write("votes")
        assert await write("board"), "a workspace page takes the write"
    async with turn(world):
        assert await write("votes"), "a person's own turn is not checked"


async def test_an_agent_shares_a_visitor_only_the_person_who_invited_them_may_add(
    world: World,
) -> None:
    """A page's settings change through the agent holds the rule the app holds: a visitor
    another member invited is added by that member or an admin, not by anyone else."""
    await put_page(world, access="people")
    async with world.factory.begin() as db:
        stranger = await make_account(db, tenant=world.tenant)
        theirs, mine = [
            await web_people.create_invited(
                db,
                person_id=uuid.uuid4(),
                tenant_id=world.tenant.id,
                account_id=(await make_account(db, tenant=world.tenant)).id,
                kind="visitor",
                display_name=name,
                workspace_name="Acme",
                email=f"{name}@example.com",
                invite_id=uuid.uuid4(),
                invited_by_account_id=inviter.id,
            )
            for name, inviter in (("theirs", stranger), ("mine", world.account))
        ]
    async with turn(world) as origin_id:
        by = await person_call(world.runtime, world.auth, origin_id)
        with pytest.raises(ToolError, match="An admin needs to share with theirs@example.com"):
            await publish_impl(
                world.runtime, world.auth, by=by, slug="votes", people=[str(theirs.id)]
            )
        page = await publish_impl(
            world.runtime, world.auth, by=by, slug="votes", people=[str(mine.id)]
        )
    assert [p.id for p in page.people] == [mine.id], "only the member's own visitor is added"


async def test_a_chat_member_who_never_opened_the_app_may_be_an_editor(world: World) -> None:
    """A member known only by their chat id is accepted; a stranger's id is not."""
    await put_page(world)
    async with world.factory.begin() as db:
        chatter = await make_account(db, tenant=world.tenant)
        await make_platform_principal(
            db, platform="discord", external_id="456", tenant=world.tenant, account=chatter
        )
    async with turn(world) as origin_id:
        by = await person_call(world.runtime, world.auth, origin_id)
        page = await publish_impl(
            world.runtime, world.auth, by=by, slug="votes", editors=["<@456>"]
        )
        with pytest.raises(ToolError, match="Not a person in the app"):
            await publish_impl(world.runtime, world.auth, by=by, slug="votes", editors=["789"])
    assert [e.id for e in page.editors] == [chatter.id], page.editors


async def test_a_hub_client_publishes_as_its_person_where_pages_are_on(
    world: World, monkeypatch
) -> None:
    """A hub call needs no chat turn: it publishes as the signed-in person, under the agent
    it names, and is refused where that agent has pages off."""
    on = SimpleNamespace(on=True)
    monkeypatch.setattr(artifact_tools, "feature_in", AsyncMock(return_value=on))
    slot = await upload_url_impl(world.runtime, world.auth, "votes.html")
    async with world.factory.begin() as db:
        await store_upload_content(
            db,
            upload_token=slot.upload_url.rsplit("/", 1)[1],
            data=HTML.encode(),
            now=datetime.now(UTC),
        )
    by = await hub_call(world.runtime, world.auth, "agent-bound")
    page = await publish_impl(
        world.runtime, world.auth, by=by, slug="votes", upload=slot.upload, title="Votes"
    )
    assert page.owner is not None and page.owner.id == world.person_id, page.owner

    on.on = False
    with pytest.raises(ToolError, match="Pages are off"):
        await hub_call(world.runtime, world.auth, "agent-bound")


async def _hold_with_outsider(world: World) -> uuid.UUID:
    """`audience_hold` on, the world's member inside it, and a guest on Discord who is not."""
    async with world.factory.begin() as db:
        await hold_audience(db, world.tenant.id)
        await upsert_member(
            db,
            tenant_id=world.tenant.id,
            platform_user_id="123",
            is_admin=False,
            now=datetime.now(UTC),
            role_ids=[INTERNAL_ROLE],
        )
        guest = await make_account(db, tenant=world.tenant)
        await link_discord_member(
            db, tenant_id=world.tenant.id, account_id=guest.id, internal=False, discord_id="456"
        )
    return guest.id


async def test_an_outsider_opens_pages_as_a_visitor_and_is_no_editor(world: World) -> None:
    """Where the audience is held, a Discord guest has no page tool, is named a reader but
    never an editor, and the page then shows it is open to people outside the workspace."""
    await put_page(world)
    guest = await _hold_with_outsider(world)
    outsider = auth_identity(guest, world.tenant.id, role=Role.USER, platform="discord")
    with pytest.raises(ToolError, match="Only members can use pages"):
        await list_impl(world.runtime, outsider)
    with pytest.raises(ToolError, match="Only members can use pages"):
        await write_impl(
            world.runtime,
            outsider,
            slug="votes",
            collection="votes",
            doc_id="a",
            data={"n": 1},
            mode="set",
        )
    assert [p.slug for p in (await list_impl(world.runtime, world.auth)).pages] == ["votes"]
    async with turn(world) as origin_id:
        by = await person_call(world.runtime, world.auth, origin_id)
        for refused in (
            publish_impl(world.runtime, world.auth, by=by, slug="votes", editors=["<@456>"]),
            transfer_impl(world.runtime, world.auth, by=by, slug="votes", person="456"),
        ):
            with pytest.raises(ToolError, match="Only members can edit|editor first"):
                await refused
        page = await publish_impl(
            world.runtime, world.auth, by=by, slug="votes", access_mode="people", people=["<@456>"]
        )
    assert page.visible_outside_workspace
    assert next(
        p for p in (await list_impl(world.runtime, world.auth)).pages
    ).visible_outside_workspace


async def test_an_owner_or_editor_turned_outsider_has_no_page_tool_until_a_member_again(
    world: World,
) -> None:
    """The member's stored roles stay, but no tool acts on them while they are an outsider."""
    await put_page(world)
    async with world.factory.begin() as db:
        other = await make_account(db, tenant=world.tenant)
        await store.publish(
            db,
            tenant_id=world.tenant.id,
            agent_id="agent-bound",
            account_id=other.id,
            is_admin=False,
            slug="theirs",
            title="Theirs",
            html=HTML,
            change=PageChange(editors=(world.account.id,)),
            origin=None,
            new_link=False,
            now=datetime.now(UTC),
            limits=LIMITS,
        )
    await _hold_with_outsider(world)

    async def standing(*role_ids: str) -> None:
        async with world.factory.begin() as db:
            await upsert_member(
                db,
                tenant_id=world.tenant.id,
                platform_user_id="123",
                is_admin=False,
                now=datetime.now(UTC),
                role_ids=list(role_ids),
            )

    async with turn(world) as origin_id:
        by = await person_call(world.runtime, world.auth, origin_id)
        await standing("9002")
        for slug in ("votes", "theirs"):
            with pytest.raises(ToolError, match="Only members can use pages"):
                await publish_impl(world.runtime, world.auth, by=by, slug=slug, title="Out")
            with pytest.raises(ToolError, match="Only members can use pages"):
                await write_impl(
                    world.runtime,
                    world.auth,
                    slug=slug,
                    collection="votes",
                    doc_id="a",
                    data={"n": 1},
                    mode="set",
                )
        await standing(INTERNAL_ROLE)
        back = [
            await publish_impl(world.runtime, world.auth, by=by, slug=slug, title="Back")
            for slug in ("votes", "theirs")
        ]
    assert [p.title for p in back] == ["Back", "Back"], "the stored roles come back"


async def _admin_token(world: World, *scopes: str) -> AuthIdentity:
    """An operator token of an admin of the world's workspace."""
    async with world.factory.begin() as db:
        admin = await make_account(db, tenant=world.tenant)
        await make_platform_principal(
            db, platform="discord", external_id="900", tenant=world.tenant, account=admin
        )
    return auth_identity(
        admin.id,
        world.tenant.id,
        role=Role.ADMIN,
        token_kind="operator",
        scopes=frozenset(scopes),
        platform="discord",
    )


async def test_an_artifacts_data_token_lists_and_reads_and_writes_but_changes_no_page(
    world: World,
) -> None:
    """The scope opens the three data tools as the admin who minted it and no others."""
    await put_page(world)
    token = await _admin_token(world, "artifacts:data")
    written = await write_impl(
        world.runtime,
        token,
        slug="votes",
        collection="votes",
        doc_id="a",
        data={"choice": "yes"},
        mode="set",
    )
    assert written.revision == 1
    seen = await query_impl(
        world.runtime,
        token,
        slug="votes",
        collection="votes",
        order_by=None,
        descending=False,
        limit=None,
    )
    assert json.dumps({"choice": "yes"}) in seen
    assert [p.slug for p in (await list_impl(world.runtime, token)).pages] == ["votes"]
    nobody: PersonCall = None  # type: ignore[assignment]
    for refused in (
        publish_impl(world.runtime, token, by=nobody, slug="votes", title="x"),
        source_impl(world.runtime, token, by=nobody, slug="votes", version=None),
        delete_impl(world.runtime, token, by=nobody, slug="votes"),
        transfer_impl(world.runtime, token, by=nobody, slug="votes", person="456"),
        upload_url_impl(world.runtime, token, "a.html"),
    ):
        with pytest.raises(ToolError, match="operator token can only"):
            await refused


async def test_an_operator_token_without_artifacts_data_is_refused(world: World) -> None:
    await put_page(world)
    token = await _admin_token(world, "tenant:read")
    with pytest.raises(ToolError, match="does not have the artifacts:data scope"):
        await list_impl(world.runtime, token)


async def test_an_artifacts_data_token_meets_the_rules_its_admin_meets(world: World) -> None:
    """The token is its admin on the page: the collection rules that bind members leave a
    manager free, so they leave the token free too."""
    await put_page(world, collection_rules={"votes": CollectionRule(read="own", write="none")})
    token = await _admin_token(world, "artifacts:data")
    written = await write_impl(
        world.runtime,
        token,
        slug="votes",
        collection="votes",
        doc_id="a",
        data={"n": 1},
        mode="set",
    )
    assert written.revision == 1
    async with world.factory.begin() as db:
        member = await make_account(db, tenant=world.tenant)
        await make_platform_principal(
            db, platform="discord", external_id="457", tenant=world.tenant, account=member
        )
    theirs = auth_identity(
        member.id, world.tenant.id, role=Role.USER, platform="discord", platform_user_id="457"
    )
    with pytest.raises(ToolError, match="read-only"):
        await write_impl(
            world.runtime,
            theirs,
            slug="votes",
            collection="votes",
            doc_id="b",
            data={"n": 1},
            mode="set",
        )

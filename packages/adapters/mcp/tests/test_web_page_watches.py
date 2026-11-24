"""`create_watch` from a web chat: the watch runs as the person's linked chat id, may be
permanent and report in new chats, and someone who doesn't manage the page holds one."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.tools import artifacts as page_tools
from daimon.adapters.mcp.tools import watches as tools
from daimon.adapters.mcp.watch_polling import PollAccessRevoked, read_page_data
from daimon.core.artifact_pages.access import PageChange
from daimon.core.artifact_pages.documents import CollectionRule, DocumentWrite
from daimon.core.config import ArtifactPagesSettings
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.membership import Membership
from daimon.core.stores import artifact_pages as store
from daimon.core.stores import watches
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.web_identity import place_of, thread_key
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from daimon.testing.watch_fixtures import install_poll_ports
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import auth_identity

pytestmark = pytest.mark.fresh_schema
KEY = "x" * 43 + "="


async def test_a_web_member_makes_a_permanent_new_chat_page_watch_and_a_viewer_holds_one(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    async with db_session_factory.begin() as db:
        await install_poll_ports(db)
        await watches.set_watch_control(db, scope="global", enabled=True)
        tenant = await make_tenant(db)
        owner, viewer = await make_account(db, tenant=tenant), await make_account(db, tenant=tenant)
        await make_platform_principal(
            db, platform="discord", external_id="d-1", tenant=tenant, account=owner
        )
        await make_platform_principal(
            db, platform="discord", external_id="v-1", tenant=tenant, account=viewer
        )
        person = await make_web_person(db, tenant=tenant, account=owner)
        await make_web_person(db, tenant=tenant, account=viewer, email="v@example.com")
        web_thread = thread_key(uuid.uuid4())
        for account, platform, thread, channel in (
            (owner, "web", web_thread, place_of(person.id)),
            (viewer, "discord", "4242", "4241"),
        ):
            await create_thread_session(
                db,
                tenant_id=tenant.id,
                platform=platform,
                thread_id=thread,
                channel_id=channel,
                account_id=account.id,
                ma_session_id=f"session-{platform}",
                ma_agent_id="agent-web",
            )
        await store.publish(
            db,
            tenant_id=tenant.id,
            agent_id="agent-web",
            account_id=owner.id,
            is_admin=False,
            slug="votes",
            title="Votes",
            html="<p>Votes</p>",
            change=PageChange(events=True, access="workspace"),
            origin=None,
            new_link=False,
            now=datetime.now(UTC),
            limits=ArtifactPagesSettings(),
        )
    web = SimpleNamespace(
        platform="web",
        parent_channel_id=place_of(person.id),
        thread_id=web_thread,
        responder_ma_agent_id="agent-web",
        responder_name="analyst",
    )
    monkeypatch.setattr(tools, "require_attended_owner", AsyncMock(return_value=web))
    monkeypatch.setattr(tools, "require_skip_defaults", AsyncMock())
    runtime = cast(
        Any,
        SimpleNamespace(
            session_factory=db_session_factory,
            membership_resolver=SimpleNamespace(
                resolve=AsyncMock(return_value=Membership(is_member=True))
            ),
            settings=SimpleNamespace(
                web=SimpleNamespace(public_url="https://pages.example"),
                crypto=SimpleNamespace(keys=(SecretStr(KEY),)),
            ),
        ),
    )

    def auth(account: Any, user: str) -> Any:
        return auth_identity(
            account.id, tenant.id, role=Role.USER, platform="discord", platform_user_id=user
        )

    made = await tools.create_watch_impl(
        runtime,
        auth(owner, "d-1"),
        origin_context_id="ctx",
        instructions="Tally the votes.",
        source="page_event",
        page="votes",
        delivery="new_thread",
        permanent=True,
        select=".data.vote",
        where={"event": ["vote", "undo"]},
    )
    async with db_session_factory() as db:
        row = await watches.get_watch(db, made.watch_id)
    assert row is not None
    assert (row.platform, row.principal_platform, row.owner_external_id) == (
        "web",
        "discord",
        "d-1",
    )
    assert (row.expires_at, row.delivery, row.rule) == (
        None,
        "new_thread",
        {"projection": ".data.vote"},
    )

    assert row.source_state == {"where": {"event": ["vote", "undo"]}}
    assert made.where == "event is vote or undo"
    with pytest.raises(ToolError, match="where"):
        await tools.create_watch_impl(
            runtime,
            auth(owner, "d-1"),
            origin_context_id="ctx",
            instructions="x",
            source="page_event",
            page="votes",
            where={"event": []},
        )
    with pytest.raises(ToolError, match="only the event fields"):
        await tools.create_watch_impl(
            runtime,
            auth(owner, "d-1"),
            origin_context_id="ctx",
            instructions="x",
            source="page_event",
            page="votes",
            where={"data.kind": "vote"},
        )

    with pytest.raises(ToolError, match="only be made in Discord"):
        await tools.create_watch_impl(
            runtime, auth(owner, "d-1"), origin_context_id="ctx", instructions="x", source="poll"
        )
    seen = ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="4241",
        thread_id="4242",
        requester_account_id=viewer.id,
        requester_external_user_id="v-1",
        target_ma_agent_id="agent-web",
        target_name="analyst",
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )

    async def watch_as_viewer() -> object:
        return await page_tools.create_page_watch(
            runtime,
            auth(viewer, "v-1"),
            seen,
            slug="votes",
            instructions="Count.",
            expires_at=None,
            max_turns_per_day=24,
            now=datetime.now(UTC),
        )

    await watch_as_viewer()
    with pytest.raises(ToolError, match="already watch"):
        await watch_as_viewer()


async def test_a_page_data_poll_reads_as_its_owner_until_they_lose_the_page(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        owner, other = await make_account(db, tenant=tenant), await make_account(db, tenant=tenant)
        page = await store.publish(
            db,
            tenant_id=tenant.id,
            agent_id="agent-web",
            account_id=owner.id,
            is_admin=False,
            slug="votes",
            title="Votes",
            html="<p>Votes</p>",
            change=PageChange(access="people"),
            origin=None,
            new_link=False,
            now=datetime.now(UTC),
            limits=ArtifactPagesSettings(),
        )
        await store.write_doc(
            db,
            page,
            "ballots",
            "b1",
            DocumentWrite(data={"vote": "yes"}),
            by="agent",
            account_id=owner.id,
            rule=None,
            now=datetime.now(UTC),
            limits=ArtifactPagesSettings(),
        )
    runtime = cast(
        Any,
        SimpleNamespace(
            session_factory=db_session_factory,
            settings=SimpleNamespace(artifact_pages=ArtifactPagesSettings()),
        ),
    )
    state = {"tool": "artifact_db_query", "arguments": {"slug": "votes", "collection": "ballots"}}

    def watch(account: Any) -> Any:
        return SimpleNamespace(tenant_id=tenant.id, owner_account_id=account.id, source_state=state)

    assert await read_page_data(runtime, watch(owner)) == {
        "rows": [{"id": "b1", "data": {"vote": "yes"}}]
    }
    with pytest.raises(PollAccessRevoked):
        await read_page_data(runtime, watch(other))


async def test_a_page_data_poll_by_a_viewer_reads_only_their_documents_under_an_own_rule(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A watch owner who only views the page is bound by its rule, as in `artifact_db_query`."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        owner, viewer = await make_account(db, tenant=tenant), await make_account(db, tenant=tenant)
        await make_web_person(db, tenant=tenant, account=viewer, email="viewer@example.com")
        page = await store.publish(
            db,
            tenant_id=tenant.id,
            agent_id="agent-web",
            account_id=owner.id,
            is_admin=False,
            slug="votes",
            title="Votes",
            html="<p>Votes</p>",
            change=PageChange(collection_rules={"ballots": CollectionRule(read="own")}),
            origin=None,
            new_link=False,
            now=datetime.now(UTC),
            limits=ArtifactPagesSettings(),
        )
        for doc_id, account in (("b1", owner), ("b2", viewer)):
            await store.write_doc(
                db,
                page,
                "ballots",
                doc_id,
                DocumentWrite(data={"by": doc_id}),
                by="agent",
                account_id=account.id,
                rule=None,
                now=datetime.now(UTC),
                limits=ArtifactPagesSettings(),
            )
    runtime = cast(
        Any,
        SimpleNamespace(
            session_factory=db_session_factory,
            settings=SimpleNamespace(artifact_pages=ArtifactPagesSettings()),
        ),
    )
    state = {"tool": "artifact_db_query", "arguments": {"slug": "votes", "collection": "ballots"}}

    def rows(account: Any) -> Any:
        return read_page_data(
            runtime,
            cast(
                Any,
                SimpleNamespace(
                    tenant_id=tenant.id, owner_account_id=account.id, source_state=state
                ),
            ),
        )

    assert await rows(viewer) == {"rows": [{"id": "b2", "data": {"by": "b2"}}]}, "own only"
    assert await rows(owner) == {
        "rows": [{"id": "b1", "data": {"by": "b1"}}, {"id": "b2", "data": {"by": "b2"}}]
    }, "the owner reads all"


@pytest.mark.parametrize("platform", ["web", "slack", "teams"])
async def test_an_admin_is_proven_only_in_a_turn_a_person_attends(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def session() -> AsyncIterator[None]:
        yield None

    runtime = cast(Any, SimpleNamespace(session_factory=session))
    account, tenant = uuid.uuid4(), uuid.uuid4()
    chat = uuid.uuid4()
    admin = auth_identity(account, tenant, platform=platform, chat_session_id=chat)
    member = auth_identity(account, tenant, Role.USER, platform=platform, chat_session_id=chat)
    unattended = SimpleNamespace(attended=False)
    attended = SimpleNamespace(attended=True, human_invocation=True)
    page_ask = SimpleNamespace(attended=True, human_invocation=False)
    lookup = AsyncMock(return_value=attended)
    monkeypatch.setattr(tools.turn_origins, "get_session_origin", lookup)
    for auth, ok in ((admin, True), (member, False)):
        raised = tools.admin_approval(
            runtime, auth, permanent=False, max_turns_per_day=48, check_ceiling=False
        )
        if ok:
            assert await raised == account
        else:
            with pytest.raises(ToolError, match="Only an admin"):
                await raised
    for refused in (unattended, page_ask):
        lookup.return_value = refused
        with pytest.raises(ToolError, match="Only an admin"):
            await tools.admin_approval(
                runtime, admin, permanent=False, max_turns_per_day=48, check_ceiling=False
            )

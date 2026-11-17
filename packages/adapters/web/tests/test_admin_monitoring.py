"""`/api/admin/monitoring`: an agent's kept conversations, the transcript, and the audit row
an admin's viewing leaves."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from daimon.core import monitoring_view
from daimon.core.stores import monitoring as store
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import FeatureTarget, MonitoredPlatform
from daimon.core.stores.domain import Role as AccountRole
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.turn_router import AGENT_ID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import WebClientFactory
from .test_admin_routes import Admin, signed_in_admin

_AT = datetime(2030, 1, 15, 12, tzinfo=UTC)
_BASE = f"/api/admin/monitoring/agents/{AGENT_ID}"


@asynccontextmanager
async def platform_admin(
    web_client: WebClientFactory, factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> AsyncIterator[Admin]:
    """The workspace owner, whose account is also a platform admin."""
    async with signed_in_admin(web_client, factory, tmp_path) as admin:
        async with factory.begin() as db:
            await set_role(db, admin.ws.person.account_id, AccountRole.ADMIN)
        yield admin


async def _keep(
    factory: async_sessionmaker[AsyncSession],
    admin: Admin,
    *,
    platform: MonitoredPlatform,
    conversation: str | None,
    at: datetime,
    kinds: tuple[str, ...] = ("question", "answer"),
    account: uuid.UUID | None = None,
    ref: str = "t1",
) -> None:
    tenant = admin.ws.tenant_id
    subject = store.MonitoredSubject(
        tenant_id=tenant,
        agent_id=monitoring_view.agent_uuid(tenant, AGENT_ID),
        platform=platform,
        account_id=account or admin.member.account_id,
        conversation=conversation,
    )
    entries = [
        store.MonitoredEntry(
            kind="tool" if kind == "tool" else kind,  # pyright: ignore[reportArgumentType]
            content=None if kind == "tool" else f"{kind} {ref}",
            tool_name="list_files" if kind == "tool" else None,
            outcome="allowed" if kind == "tool" else None,
            turn_ref=None if kind == "tool" else ref,
            created_at=at + timedelta(seconds=i),
        )
        for i, kind in enumerate(kinds)
    ]
    async with factory.begin() as db:
        await store.record_events(db, subject, entries)


async def test_conversations_are_listed_newest_first_with_who_and_where(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with platform_admin(web_client, db_session_factory, tmp_path) as admin:
        await _keep(db_session_factory, admin, platform="web", conversation="w1", at=_AT)
        await _keep(
            db_session_factory,
            admin,
            platform="slack",
            conversation="dm1",
            at=_AT + timedelta(hours=1),
        )
        await _keep(
            db_session_factory,
            admin,
            platform="mcp",
            conversation=None,
            kinds=("tool", "tool"),
            at=_AT + timedelta(hours=2),
        )
        listed = (await admin.client.get(f"{_BASE}/conversations")).json()
        only_web = (await admin.client.get(f"{_BASE}/conversations?where=web")).json()
        other = (
            await admin.client.get(f"{_BASE}/conversations?person={admin.ws.person.id}")
        ).json()

    assert [(r["where"], r["questions"], r["tools"]) for r in listed] == [
        ("coding_tools", 0, 2),
        ("direct_messages", 1, 0),
        ("web", 1, 0),
    ], "most recent first; a person's tool calls make one row"
    assert listed[0]["person"] == {"id": str(admin.member.id), "name": "Mia"}
    assert [r["where"] for r in only_web] == ["web"], "the where filter keeps one place"
    assert other == [], "a person with nothing kept has no rows"


async def test_conversations_filter_by_day_and_page_with_a_cursor(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with platform_admin(web_client, db_session_factory, tmp_path) as admin:
        for n in range(3):
            await _keep(
                db_session_factory,
                admin,
                platform="web",
                conversation=f"w{n}",
                at=_AT + timedelta(days=n),
            )
        day = await admin.client.get(f"{_BASE}/conversations?from=2030-01-16&to=2030-01-16")
        first = await admin.client.get(f"{_BASE}/conversations?limit=2")
        second = await admin.client.get(
            f"{_BASE}/conversations?limit=2&cursor={first.headers['X-Next-Cursor']}"
        )

    assert len(day.json()) == 1, "a day range keeps that day's conversations"
    assert len(first.json()) == 2 and len(second.json()) == 1, "the cursor continues the list"
    assert "X-Next-Cursor" not in second.headers, "the last page names no next page"


async def test_a_transcript_reads_in_order_and_a_tool_day_shows_its_calls(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with platform_admin(web_client, db_session_factory, tmp_path) as admin:
        await _keep(db_session_factory, admin, platform="web", conversation="w1", at=_AT)
        await _keep(
            db_session_factory,
            admin,
            platform="web",
            conversation="w1",
            at=_AT + timedelta(minutes=5),
            ref="t2",
        )
        await _keep(
            db_session_factory,
            admin,
            platform="mcp",
            conversation=None,
            kinds=("tool",),
            at=_AT + timedelta(hours=1),
        )
        rows = {
            r["where"]: r["id"] for r in (await admin.client.get(f"{_BASE}/conversations")).json()
        }
        web = (await admin.client.get(f"{_BASE}/conversations/{rows['web']}")).json()
        tools = (await admin.client.get(f"{_BASE}/conversations/{rows['coding_tools']}")).json()

    assert [(e["kind"], e["content"]) for e in web] == [
        ("question", "question t1"),
        ("answer", "answer t1"),
        ("question", "question t2"),
        ("answer", "answer t2"),
    ], "questions and answers in the order they happened"
    assert [(e["kind"], e["tool"], e["outcome"]) for e in tools] == [
        ("tool", "list_files", "allowed")
    ]


async def test_viewing_the_list_and_a_transcript_is_audited_once_per_open(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with platform_admin(web_client, db_session_factory, tmp_path) as admin:
        await _keep(db_session_factory, admin, platform="web", conversation="w1", at=_AT)
        [row] = (await admin.client.get(f"{_BASE}/conversations")).json()
        await admin.client.get(f"{_BASE}/conversations/{row['id']}")
        await admin.client.get(_BASE)
        trail = await admin.trail(db_session_factory)

    assert [(action, detail) for action, _, detail in trail] == [
        ("Viewed monitored activity", None),
        ("Opened monitored conversation", "Mia"),
    ], "the list and the transcript are each audited; the switches are not content"


async def test_the_switches_say_since_when_each_is_on(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with platform_admin(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            await set_feature_value(
                db,
                tenant_id=admin.ws.tenant_id,
                target=FeatureTarget("agent", agent_id=AGENT_ID),
                key="monitor_mcp",
                value="on",
                set_by=None,
                surface="web",
            )
        state = (await admin.client.get(_BASE)).json()

    assert state["direct_messages"] == {"on": False, "since": None}, "off by default"
    assert state["coding_tools"]["on"] is True and state["coding_tools"]["since"], (
        "on, with when it was turned on"
    )


async def test_an_unknown_agent_or_a_bad_handle_is_not_found(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with platform_admin(web_client, db_session_factory, tmp_path) as admin:
        agent = await admin.client.get("/api/admin/monitoring/agents/ag_nope/conversations")
        handle = await admin.client.get(f"{_BASE}/conversations/not-a-handle")
        trail = await admin.trail(db_session_factory)

    assert (agent.status_code, handle.status_code) == (404, 404), "nothing is kept for either"
    assert trail == [], "a refused read leaves no audit row"


async def test_a_workspace_owner_who_is_no_platform_admin_reads_nothing(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The bar is the switch's and the chat tools': a web grant alone is refused, unaudited."""
    async with signed_in_admin(web_client, db_session_factory, tmp_path) as admin:
        await _keep(db_session_factory, admin, platform="web", conversation="w1", at=_AT)
        paths = (_BASE, f"{_BASE}/conversations", f"{_BASE}/conversations/w1")
        refused = [await admin.client.get(path) for path in paths]
        trail = await admin.trail(db_session_factory)

    assert [r.status_code for r in refused] == [403, 403, 403]
    assert refused[1].json()["detail"] == "Only platform admins can read monitored conversations."
    assert trail == [], "a refused read leaves no audit row"

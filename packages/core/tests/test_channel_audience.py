"""`channel_has_outsiders` answers as the sticky registry does; `audience_hold` is read by value."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest
from daimon.core.channel_audience import (
    ChannelAudience,
    audience_mode,
    channel_has_outsiders,
    channels_with_outsiders,
)
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores.audience_facts import StoredRoom
from daimon.core.stores.domain import FeatureTarget, TenantRow
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.shared_channels import is_channel_shared, mark_channel_shared
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.factories import make_tenant
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

_ROOT = Path(__file__).resolve().parents[3]


async def test_a_recorded_channel_is_contained_and_any_other_is_open_as_the_registry_says(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-1")
    await mark_channel_shared(
        db_session, tenant_id=tenant.id, platform="discord", channel_id="C1", source="client"
    )
    answer = ChannelAudience(contained=True, known=True, narrow=False, source="registry")
    for channel_id, contained in (("C1", True), ("C2", False)):
        answer = await channel_has_outsiders(
            db_session, tenant_id=tenant.id, platform="discord", channel_id=channel_id
        )
        assert answer.contained is contained
        assert answer.contained is await is_channel_shared(
            db_session, tenant_id=tenant.id, platform="discord", channel_id=channel_id
        )
    assert answer == ChannelAudience(contained=False, known=False, narrow=False, source="none")
    assert await channels_with_outsiders(db_session, tenant_id=tenant.id, platform="discord") == {
        "C1"
    }


async def test_the_mode_is_off_until_a_server_sets_it_and_shadow_is_not_on(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-1")
    place = FeaturePlace(tenant_id=tenant.id, platform="discord", channel_id="C1")
    assert await audience_mode(db_session, place) == "off"
    await set_feature_value(
        db_session,
        tenant_id=tenant.id,
        target=FeatureTarget("workspace"),
        key="audience_hold",
        value="shadow",
        set_by=None,
        surface="chat",
    )
    assert await audience_mode(db_session, place) == "shadow", "a write drops the stored read"

    reads: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        reads.append(statement)

    engine = db_session.bind.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        other_room = FeaturePlace(tenant_id=tenant.id, platform="discord", channel_id="C2")
        assert await audience_mode(db_session, other_room) == "shadow"
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert reads == [], "a transaction reads a server's mode once, for every room"


async def test_a_failed_mode_read_raises_instead_of_picking_a_mode(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("database is down")

    monkeypatch.setattr("daimon.core.channel_audience.feature_in", broken)
    place = FeaturePlace(tenant_id=uuid.uuid4(), platform="discord")
    with pytest.raises(RuntimeError, match="database is down"):
        await audience_mode(db_session, place)


def test_no_code_reads_audience_hold_through_dot_on() -> None:
    """`shadow` is on for `.on`. Every reader of this key compares `.value`."""
    offenders: list[str] = []
    for base in (_ROOT / "packages/core/daimon", *(_ROOT / "packages/adapters").glob("*/daimon")):
        for path in base.rglob("*.py"):
            text = path.read_text()
            if "audience_hold" not in text:
                continue
            for func in ast.walk(ast.parse(text)):
                if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if "audience_hold" not in (ast.get_source_segment(text, func) or ""):
                    continue
                offenders += [
                    f"{path.name}:{node.lineno}"
                    for node in ast.walk(func)
                    if isinstance(node, ast.Attribute) and node.attr == "on"
                ]
    assert not offenders, f"read audience_hold with .on (shadow counts as on): {offenders}"


_REGISTRY_READS = frozenset({"is_channel_shared", "list_shared_channel_ids"})
# The audience module and the two stores that define the reads.
_REGISTRY_READERS_ALLOWED = frozenset(
    {
        "packages/core/daimon/core/channel_audience.py",
        "packages/core/daimon/core/stores/shared_channels.py",
        "packages/core/daimon/core/stores/channel_directory.py",
    }
)


def test_every_hold_reader_asks_channel_has_outsiders_and_none_reads_the_registry_directly() -> (
    None
):
    """Admission, `/memory`, routines and the channel list must not go around the one question."""
    offenders: list[str] = []
    for path in sorted((_ROOT / "packages").glob("*/**/daimon/**/*.py")):
        rel = path.relative_to(_ROOT).as_posix()
        if "/tests/" in rel or rel in _REGISTRY_READERS_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else next((a.name for a in node.names if a.name in _REGISTRY_READS), None)
                if isinstance(node, ast.ImportFrom)
                else None
            )
            if name in _REGISTRY_READS:
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


# Standing and the facts source (fictional ids) -------------------------------------------

ROOM = "7001"
THREAD = "7002"
USER = "8001"
INTERNAL = "9001"


async def _internal_server(db_session: AsyncSession) -> TenantRow:
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy

    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-9")
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(internal_role_ids=(INTERNAL,))
    )
    return tenant


async def _publish(
    db_session: AsyncSession, tenant: TenantRow, rooms: dict[str, StoredRoom]
) -> None:
    from datetime import UTC, datetime, timedelta

    from daimon.core.stores.audience_facts import publish_facts

    now = datetime.now(UTC)
    await publish_facts(
        db_session, tenant_id=tenant.id, rooms=rooms, now=now, fresh_until=now + timedelta(hours=1)
    )


async def test_facts_answer_contained_unknown_and_thread_fallback_in_mode_on(
    db_session: AsyncSession,
) -> None:
    from daimon.core.audience_facts import RoomFacts

    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-9")
    await _publish(
        db_session,
        tenant,
        {
            ROOM: StoredRoom(RoomFacts(contained=True, narrow=False)),
            "7003": StoredRoom(RoomFacts(contained=False, narrow=True)),
        },
    )

    async def ask(channel: str, thread: str | None = None, mode: str = "on") -> ChannelAudience:
        return await channel_has_outsiders(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=channel,
            thread_id=thread,
            mode=mode,  # pyright: ignore[reportArgumentType]
        )

    contained = await ask(ROOM)
    assert (contained.contained, contained.known, contained.source) == (True, True, "facts")
    assert (await ask(ROOM, THREAD)).contained, "a thread reads its parent"
    assert (await ask("7003")).narrow and not (await ask("7003")).contained
    unknown = await ask("7999")
    assert (unknown.contained, unknown.narrow, unknown.known) == (True, True, False)
    shadow = await ask(ROOM, mode="shadow")
    assert not shadow.contained and shadow.shadow is not None and shadow.shadow.contained
    assert not (await ask(ROOM, mode="off")).contained
    slack = await channel_has_outsiders(
        db_session, tenant_id=tenant.id, platform="slack", channel_id=ROOM, mode="on"
    )
    assert slack.source != "facts", "Slack keeps the sticky registry"


async def test_standing_follows_event_roles_then_mirror_then_one_fetch_then_outsider(
    db_session: AsyncSession,
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from daimon.core.channel_audience import requester_standing
    from daimon.core.membership import Membership
    from daimon.core.stores.workspace_members import upsert_member

    tenant = await _internal_server(db_session)

    async def standing(**kw: Any) -> RequesterOrg:
        return await requester_standing(
            db_session, tenant_id=tenant.id, user_id=USER, mode="on", **kw
        )

    assert await standing(event_role_ids=(INTERNAL,)) is RequesterOrg.HOST
    assert await standing(event_role_ids=("1",)) is RequesterOrg.OUTSIDER

    fetch = AsyncMock()
    fetch.resolve.return_value = Membership(is_member=True, role_ids=(INTERNAL,))
    assert await standing(resolver=fetch) is RequesterOrg.HOST, "cleared mirror: one fetch"
    assert fetch.resolve.await_count == 1
    assert fetch.resolve.await_args.kwargs["require_fresh"] is True

    await upsert_member(
        db_session,
        tenant_id=tenant.id,
        platform_user_id=USER,
        is_admin=False,
        now=datetime.now(UTC),
        role_ids=("1",),
    )
    assert await standing(resolver=fetch) is RequesterOrg.OUTSIDER, "the mirror comes first"
    assert fetch.resolve.await_count == 1

    assert await standing(event_role_ids=None, resolver=None) is RequesterOrg.OUTSIDER


async def test_a_server_with_no_internal_roles_and_mode_off_give_host(
    db_session: AsyncSession,
) -> None:
    from daimon.core.channel_audience import requester_standing

    plain = await make_tenant(db_session, platform="discord", workspace_id="g-8")
    server = await _internal_server(db_session)
    kw: dict[str, Any] = {"user_id": USER, "event_role_ids": ("1",)}
    assert (
        await requester_standing(db_session, tenant_id=plain.id, mode="on", **kw)
        is RequesterOrg.HOST
    )
    assert (
        await requester_standing(db_session, tenant_id=server.id, mode="off", **kw)
        is RequesterOrg.HOST
    )


async def test_shadow_standing_logs_the_outsider_and_changes_nothing(
    db_session: AsyncSession,
) -> None:
    import structlog
    from daimon.core.channel_audience import requester_standing

    tenant = await _internal_server(db_session)
    with structlog.testing.capture_logs() as logs:
        org = await requester_standing(
            db_session,
            tenant_id=tenant.id,
            user_id=USER,
            channel_id=ROOM,
            is_dm=True,
            event_role_ids=("1",),
            mode="shadow",
        )
    assert org is RequesterOrg.HOST
    event = next(e for e in logs if e["event"] == "audience.shadow")
    assert (event["kind"], event["channel_id"]) == ("dm_refused", ROOM)
    assert set(event) == {
        "event",
        "log_level",
        "kind",
        "tenant_id",
        "channel_id",
        "agent",
        "reason",
    }

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
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.shared_channels import is_channel_shared, mark_channel_shared
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

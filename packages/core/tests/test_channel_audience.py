"""`channel_has_outsiders` answers as the sticky registry does; `audience_hold` is read by value."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

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
    assert await audience_mode(db_session, place) == "shadow"


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

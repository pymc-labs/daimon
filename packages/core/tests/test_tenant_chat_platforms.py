"""Where a tenant's agents answer: its chat platforms, and a feature read across all of them."""

from __future__ import annotations

import pytest
from daimon.core.features import snapshot
from daimon.core.features.snapshot import deployment_on_everywhere
from daimon.core.stores.identity import (
    get_or_create_cli_principal,
    get_or_create_platform_principal,
    list_tenant_chat_platforms,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_a_tenant_answers_on_the_chat_platforms_it_has_people_on(
    db_session: AsyncSession,
) -> None:
    """Chat platforms of the tenant's principals, sorted; the CLI and other tenants don't count."""
    tenant = await make_tenant(db_session, platform="discord")
    other = await make_tenant(db_session, platform="teams")
    for platform, external_id in (("slack", "1"), ("discord", "2"), ("slack", "3")):
        await get_or_create_platform_principal(
            db_session, tenant_id=tenant.id, platform=platform, external_id=external_id
        )
    await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="me")
    await get_or_create_platform_principal(
        db_session, tenant_id=other.id, platform="teams", external_id="9"
    )
    assert await list_tenant_chat_platforms(db_session, tenant_id=tenant.id) == (
        "discord",
        "slack",
    ), "each chat platform once, in order, for this tenant only"


def test_a_feature_counts_as_on_only_where_every_platform_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strictest reading: one platform that turns it off turns it off."""
    monkeypatch.setattr(snapshot, "deployment_on", lambda _s, _k, platform: platform != "teams")
    assert deployment_on_everywhere(object(), "x", ("discord", "slack")), "on wherever asked"  # pyright: ignore[reportArgumentType]
    assert not deployment_on_everywhere(object(), "x", ("discord", "teams")), "off on teams"  # pyright: ignore[reportArgumentType]

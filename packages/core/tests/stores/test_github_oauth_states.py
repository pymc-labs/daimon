"""Integration tests for github_oauth_states GDPR helpers — real Postgres.

Phase 97 (D-03/C-01) removed the OAuth-flow write path (`create`/`peek`/
`get_by_state`/`consume`) — the store now retains only the erasure helpers
`delete_states_for_platform_user` / `count_states_for_platform_user`, used by
`daimon.core.privacy` / `daimon.core.purge` to purge legacy PII rows. Rows are
seeded directly via the `make_oauth_state` factory (ORM insert), not through
a removed store write path.
"""

from __future__ import annotations

import uuid

from daimon.core._models import Tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import github_oauth_states as store
from sqlalchemy.ext.asyncio import AsyncSession

from ..factories.github import make_oauth_state


async def _seed_tenant(session: AsyncSession) -> uuid.UUID:
    guild_id = str(uuid.uuid4())
    t = Tenant(
        id=derive_tenant_uuid(platform="discord", workspace_id=guild_id),
        platform="discord",
        external_id=guild_id,
    )
    session.add(t)
    await session.flush()
    return t.id


async def test_delete_states_for_platform_user_removes_live_consumed_and_expired_rows(
    db_session: AsyncSession,
) -> None:
    """Delete must remove live, consumed, AND expired-looking rows — all carry PII."""
    tenant_id = await _seed_tenant(db_session)
    platform = "discord"
    platform_user_id = "pii-user"

    await make_oauth_state(
        db_session,
        tenant_id=tenant_id,
        platform=platform,
        platform_user_id=platform_user_id,
    )
    await make_oauth_state(
        db_session,
        tenant_id=tenant_id,
        platform=platform,
        platform_user_id=platform_user_id,
        consumed=True,
    )
    await make_oauth_state(
        db_session,
        tenant_id=tenant_id,
        platform=platform,
        platform_user_id=platform_user_id,
        age_minutes=15,
    )

    rowcount = await store.delete_states_for_platform_user(
        db_session,
        platform=platform,
        platform_user_id=platform_user_id,
    )

    assert rowcount == 3, (
        "delete must remove all 3 rows (live + consumed + expired) — "
        "consumed and expired rows still carry platform_user_id PII"
    )


async def test_delete_states_for_platform_user_leaves_other_users_untouched(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed_tenant(db_session)
    platform = "discord"

    await make_oauth_state(
        db_session, tenant_id=tenant_id, platform=platform, platform_user_id="target-user"
    )
    await make_oauth_state(
        db_session, tenant_id=tenant_id, platform=platform, platform_user_id="other-user"
    )

    await store.delete_states_for_platform_user(
        db_session, platform=platform, platform_user_id="target-user"
    )

    count_other = await store.count_states_for_platform_user(
        db_session, platform=platform, platform_user_id="other-user"
    )
    assert count_other == 1, "other-user's oauth-state row must survive the delete"


async def test_delete_states_for_platform_user_scoped_by_tenant_for_colliding_ids(
    db_session: AsyncSession,
) -> None:
    """A Slack user id is workspace-scoped, so the same external_id in two
    tenants are two different humans. Purging one with tenant_id set must not
    delete the other tenant's identically-named user's oauth-state rows."""
    tenant_a = await _seed_tenant(db_session)
    tenant_b = await _seed_tenant(db_session)
    platform = "slack"

    for tenant_id in (tenant_a, tenant_b):
        await make_oauth_state(
            db_session, tenant_id=tenant_id, platform=platform, platform_user_id="U123"
        )

    deleted = await store.delete_states_for_platform_user(
        db_session, platform=platform, platform_user_id="U123", tenant_id=tenant_a
    )
    assert deleted == 1, "only tenant_a's U123 handshake row should be deleted"

    count_b = await store.count_states_for_platform_user(
        db_session, platform=platform, platform_user_id="U123"
    )
    assert count_b == 1, "tenant_b's identically-named user's row must survive"


async def test_count_states_for_platform_user_matches_delete_rowcount(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed_tenant(db_session)
    platform = "discord"
    platform_user_id = "count-test-user"

    for _ in range(3):
        await make_oauth_state(
            db_session, tenant_id=tenant_id, platform=platform, platform_user_id=platform_user_id
        )

    count_before = await store.count_states_for_platform_user(
        db_session, platform=platform, platform_user_id=platform_user_id
    )
    deleted = await store.delete_states_for_platform_user(
        db_session, platform=platform, platform_user_id=platform_user_id
    )

    assert count_before == deleted, (
        "count helper must equal the rowcount delete returns for the same seed (parity)"
    )
    assert count_before == 3, "expected 3 seeded rows"

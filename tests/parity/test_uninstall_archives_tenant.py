"""Scenario (g): uninstall -> tenant archived_at stamped + platform-scoped
ephemeral rows absent (empty expected set on Discord by design), on both
platforms.

Divergence principle (05-CONTEXT D-10): Slack's `teardown_slack_install`
additionally deletes 3 workspace-scoped ephemeral tables (slack_bot_tokens,
slack_connect_prompts, slack_event_dedup); Discord's `uninstall` only stamps
`archived_at` -- no Discord behavior change here, so there is nothing else to
assert on that platform. The divergence is encoded in `_assert_platform_effects`
below rather than forced into a shared, platform-symmetric assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from cryptography.fernet import Fernet
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.domain import Platform
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token, upsert_slack_bot_token
from daimon.core.stores.slack_connect_prompts import (
    delete_connect_prompts_for_team,
    mark_connect_prompted,
)
from daimon.core.stores.slack_event_dedup import delete_event_dedup_for_team, insert_if_new
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .drivers.protocol import PlatformDriver


async def _assert_platform_effects(
    driver: PlatformDriver,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
) -> None:
    """Divergence hook: Slack's uninstall additionally clears 3 workspace-
    scoped ephemeral tables; Discord's expected ephemeral set is empty by
    design, so there is nothing further to assert on that platform."""
    if driver.param_id != "slack":
        return
    async with session_factory() as s:
        assert await get_slack_bot_token(s, team_id=workspace_id) is None, (
            "slack: bot token must be deleted after uninstall"
        )
        assert await delete_connect_prompts_for_team(s, team_id=workspace_id) == 0, (
            "slack: connect prompts must already be gone after uninstall"
        )
        assert await delete_event_dedup_for_team(s, team_id=workspace_id) == 0, (
            "slack: event dedup rows must already be gone after uninstall"
        )
        await s.commit()


# Removing the Teams app archives nothing (test_teams_deliberate_gaps.py).
@pytest.mark.parametrize("driver", ["discord", "slack"], indirect=True)
async def test_uninstall_archives_tenant(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = f"uninstall-{driver.param_id}"
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    assert tenant.archived_at is None, "sanity: tenant starts unarchived"

    if driver.param_id == "slack":
        fernet = build_multifernet((Fernet.generate_key().decode(),))
        await upsert_slack_bot_token(
            db_session,
            team_id=workspace_id,
            encrypted_token=encrypt_token(fernet, "xoxb-uninstall-test"),
        )
        await mark_connect_prompted(
            db_session,
            team_id=workspace_id,
            slack_user_id="U_UNINSTALL",
            now=datetime.now(UTC),
        )
        await insert_if_new(db_session, team_id=workspace_id, channel="C_UNINSTALL", event_ts="1.0")
    await db_session.commit()

    await driver.uninstall(sessionmaker=db_session_factory, workspace_id=workspace_id)

    async with db_session_factory() as s:
        post = await get_tenant(s, tenant.id)
    assert post is not None, f"{driver.param_id}: tenant row must still exist after uninstall"
    assert post.archived_at is not None, (
        f"{driver.param_id}: uninstall must stamp tenant.archived_at"
    )

    await _assert_platform_effects(driver, db_session_factory, workspace_id=workspace_id)

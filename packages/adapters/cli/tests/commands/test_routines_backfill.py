"""Tests for `daimon routines backfill-agent-names`.

Migration 0012 flipped `routines.agent_name` to NOT NULL — no row can be in
the "missing agent_name" state any more, so the backfill is now a permanent
no-op that operators no longer need to run. These tests verify that the
command runs cleanly when there is nothing to do.

History: the rich behavior matrix (metadata-lookup, 404 fallback, archived
fallback, idempotency, dry-run, 5xx, missing-daimon-name fallback) was
exercised by the previous version of this file before 0012 landed; the code
path it tested is no longer reachable from a real database.
"""

from __future__ import annotations

from io import StringIO
from typing import cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands.routines import run_backfill_agent_names
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _build_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> CliRuntime:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client)
    fake_settings = cast(Settings, object())
    return CliRuntime(
        settings=fake_settings,
        anthropic=client,
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


@pytest.mark.asyncio
async def test_backfill_is_a_no_op_post_0012(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After 0012 NOT NULL, no row can have agent_name=NULL, so backfill makes
    zero MA calls and exits cleanly.
    """
    # Seed one normal row (agent_name already set, as the column requires).
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s)
        await create_routine(
            s,
            created_by_user_id="u1",
            agent_id="agent_aaa",
            agent_name="daimon",
            cron_expr="*/5 * * * *",
            timezone_="UTC",
            trigger_message="ping",
            tenant_id=tenant.id,
        )

    # Router with no handlers: any MA call would raise (no route matches).
    router = MARouter()
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=200)
    rt = _build_rt(db_session_factory, router)

    # Must not raise: list_routines_missing_agent_name returns [] post-0012.
    await run_backfill_agent_names(rt=rt, console=console, dry_run=False)

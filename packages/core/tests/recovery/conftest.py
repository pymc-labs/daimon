"""Fixtures for the restart-and-replay matrix."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.recovery import Process, ProviderWorld, restarted
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ..turn_path_support import TurnPath


@dataclass
class Scenario:
    """A tenant with funds, a provider that outlives processes, and the live process."""

    tenant_id: Any
    account_id: Any
    world: ProviderWorld
    engine: AsyncEngine
    tmp_path: Path
    path: TurnPath
    process: Process

    def restart(self) -> Process:
        """Replace the process with a fresh one; the old one is left as it died."""
        self.process = restarted(self.process, self.path, tmp_path=self.tmp_path)
        return self.process


@pytest.fixture
async def scenario(
    db_session: AsyncSession,
    db_committing_engine: AsyncEngine,
    tmp_path: Path,
    turn_path: TurnPath,
) -> Scenario:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    account = await make_account(db_session, tenant=tenant)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()
    agent = ma_agent(id="ag_recovery", name="daimon", tenant_id=tenant.id)
    environment = ma_environment(id="env_recovery", name="default", tenant_id=tenant.id)
    world = ProviderWorld(agent=agent, environment=environment, tenant_id=tenant.id)
    process = Process.start(world, db_committing_engine, turn_path, tmp_path=tmp_path)
    return Scenario(
        tenant_id=tenant.id,
        account_id=account.id,
        world=world,
        engine=db_committing_engine,
        tmp_path=tmp_path,
        path=turn_path,
        process=process,
    )


Factory = Callable[..., Any]

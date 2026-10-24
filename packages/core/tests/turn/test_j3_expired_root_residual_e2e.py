"""J3/J4/J10: a fleet root whose turn is over and whose usage is all on the ledger no
longer holds its cash against the next root or fleet.

SMOKE fc0fa16a: besides the finished runs, 22 fleet roots from 10-15..10-18 still held
$2.52 (exposure, debit 0) of the staging test tenant's balance, long past their turns.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from daimon.core.channel_budget import _financial_room
from daimon.core.stores import fleet, mux_state

from .test_frozen_root import root_turn as root_turn
from .test_root_funding import controls, credit, funded


@pytest.mark.parametrize("expired", [True, False])
async def test_an_expired_settled_root_does_not_block_the_next(root_turn, expired):
    deps, _, _, _, _, scope, binding, _ = root_turn
    await credit(root_turn)
    deadline = datetime.now(UTC) + (timedelta(minutes=-1) if expired else timedelta(hours=1))
    await funded(
        root_turn,
        initialize=True,
        selected={**controls(root_turn), "deadline": deadline.isoformat()},
    )
    async with deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db, tenant_id=UUID(scope.tenant_id), actor_id=UUID(scope.account_id), run_id=None
        )
        roots = await mux_state.list_fleet_root_finances(db, tenant_id=UUID(scope.tenant_id))
        assert len(roots) == 1 and roots[0].result["root_finance"]["exposure_usd"] == "0.06"
        available, _ = await _financial_room(
            db,
            tenant_id=UUID(scope.tenant_id),
            platform=binding.thread.channel.platform,
            channel_id=binding.thread.channel.channel_id,
            graph=graph,
            roots=roots,
        )
    # Over and settled: its cash returns. Still within its turn: it is held.
    assert available == (Decimal("0.06") if expired else Decimal(0))

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
from mux.state.store import binding_slot

from .test_frozen_root import root_turn as root_turn
from .test_root_funding import controls, credit, funded


async def _root(root_turn, *, expired: bool, processed: bool):
    deps, _, _, _, _, scope, binding, _ = root_turn
    await credit(root_turn)
    deadline = datetime.now(UTC) + (timedelta(minutes=-1) if expired else timedelta(hours=1))
    original = await funded(
        root_turn,
        initialize=True,
        selected={**controls(root_turn), "deadline": deadline.isoformat()},
    )
    if processed:
        # The root's input went out and was answered: its turn has a terminal record.
        now = original.operation.created_at
        lease = await deps.state_store.acquire_lease(
            binding_slot(binding), holder="root", turn_id="t", now=now, ttl=timedelta(minutes=5)
        )
        try:
            await deps.state_store.claim_send(scope, original.operation.key, now=now, fence=lease)
            await deps.state_store.advance_operation(
                scope, original.operation.key, "processed", now=now, fence=lease
            )
        finally:
            await deps.state_store.release_lease(lease)


async def _available(root_turn) -> Decimal | None:
    deps, _, _, _, _, scope, binding, _ = root_turn
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
    return available


@pytest.mark.parametrize(
    ("expired", "processed", "released"),
    [
        (True, True, True),  # over, answered, settled: its cash returns
        (False, True, False),  # still within its turn
        (True, False, False),  # past its deadline, but no terminal record of its input
    ],
)
async def test_an_expired_settled_root_does_not_block_the_next(
    root_turn, expired, processed, released
):
    await _root(root_turn, expired=expired, processed=processed)
    assert await _available(root_turn) == (Decimal("0.06") if released else Decimal(0))


async def test_an_expired_root_with_pending_usage_stays_held(root_turn):
    """Usage not yet on the ledger is unknown spend."""
    from daimon.core._models import AccountingOutbox
    from sqlalchemy import insert

    deps, _, _, _, _, scope, binding, _ = root_turn
    await _root(root_turn, expired=True, processed=True)
    async with deps.sessionmaker.begin() as db:
        await db.execute(
            insert(AccountingOutbox).values(
                binding_id=binding.id,
                observation_id="pending-meter",
                revision=1,
                prior_applied_revision=None,
                tenant_id=UUID(scope.tenant_id),
                row={},
            )
        )
    assert await _available(root_turn) == Decimal(0)

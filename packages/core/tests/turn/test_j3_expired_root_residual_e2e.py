"""J3/J4/J10: a fleet root whose turn is over and whose usage is all on the ledger no
longer holds its cash against the next root or fleet.

SMOKE fc0fa16a: besides the finished runs, 22 fleet roots from 10-15..10-18 still held
$2.52 (exposure, debit 0) of the staging test tenant's balance, long past their turns.
A past deadline alone doesn't end a root turn (its `fleet_root:` operation is processed
at run setup, before any model call): the turn must also be over, with no live lease
on the root's slot and no unanswered send on its native session.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from daimon.core._models import AccountingOutbox, MuxOperation
from daimon.core.channel_budget import _financial_room
from daimon.core.stores import fleet, mux_state
from mux.state.store import binding_slot
from sqlalchemy import func, insert

from .test_frozen_root import root_turn as root_turn
from .test_root_funding import controls, credit, funded


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
    ("expired", "still", "released"),
    [
        (True, None, True),  # over, nothing in flight, settled: its cash returns
        (False, None, False),  # still within its turn
        (True, "lease", False),  # past its deadline, but its slot is still leased
        (True, "unanswered_send", False),  # past its deadline, a send still unanswered
        (True, "pending_usage", False),  # usage not yet on the ledger is unknown spend
    ],
)
async def test_an_expired_root_releases_only_once_its_turn_is_over(
    root_turn, expired, still, released
):
    deps, _, _, _, _, scope, binding, _ = root_turn
    await credit(root_turn)
    deadline = datetime.now(UTC) + (timedelta(minutes=-1) if expired else timedelta(hours=1))
    await funded(
        root_turn,
        initialize=True,
        selected={**controls(root_turn), "deadline": deadline.isoformat()},
    )
    if still == "lease":
        await deps.state_store.acquire_lease(
            binding_slot(binding),
            holder="root",
            turn_id="current",
            now=datetime.now(UTC),
            ttl=timedelta(minutes=5),
        )
    async with deps.sessionmaker.begin() as db:
        if still == "unanswered_send":
            await db.execute(
                insert(MuxOperation).values(
                    tenant_id=UUID(scope.tenant_id),
                    account_id=scope.account_id,
                    key=f"turn:{binding.native_refs['session']}:current:send:0",
                    principal_id=scope.account_id,
                    status="sent",
                    record={},
                    updated_at=func.now(),
                )
            )
        if still == "pending_usage":
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
    assert await _available(root_turn) == (Decimal("0.06") if released else Decimal(0))

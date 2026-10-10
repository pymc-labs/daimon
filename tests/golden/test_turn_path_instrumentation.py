"""Child-only probe for the real oracle's mocked run-turn recorder."""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from daimon.testing.effect_recorder import database_metadata
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def test_controls_are_forwarded_without_erasing_semantic_kwargs() -> None:
    turn = AsyncMock(name="run_turn", return_value=None)
    backend, scope, session_ref = object(), object(), object()
    await turn(
        path="mux",
        backend=backend,
        scope=scope,
        session_ref=session_ref,
        session_id="sess_probe",
        user_message="Literal /notes/file_alpha",
        model_id="caller-model",
        price=Decimal("0.0000001234"),
        continuity="history",
    )
    # Filtering is a recorder policy; the actual caller still receives all DI.
    assert turn.call_args.kwargs["backend"] is backend
    assert turn.call_args.kwargs["scope"] is scope
    assert turn.call_args.kwargs["session_ref"] is session_ref
    assert turn.call_args.kwargs["path"] == "mux"


@pytest.mark.parametrize("lease_count", (0, 1, 3))
async def test_lease_inserts_preserve_host_uuid_sequence(
    db_session: AsyncSession, lease_count: int
) -> None:
    metadata = database_metadata()
    tenants = metadata.tables["tenants"]
    leases = metadata.tables["thread_lease"]
    before = uuid.uuid4()
    tenant_id = await db_session.scalar(
        insert(tenants).values(platform="discord", external_id="uuid-probe").returning(tenants.c.id)
    )
    assert tenant_id == uuid.UUID(int=before.int + 1)
    for index in range(lease_count):
        lease_id = await db_session.scalar(
            insert(leases)
            .values(
                tenant_id=tenant_id,
                platform="discord",
                channel_id="uuid-probe",
                thread_id=str(index),
            )
            .returning(leases.c.id)
        )
        assert lease_id == uuid.UUID(int=(1 << 120) + index + 1)
    # Direct controls and a second host-table server default keep their ordinals.
    assert uuid.uuid4() == uuid.UUID(int=before.int + 2)
    assert await db_session.scalar(
        insert(tenants)
        .values(platform="discord", external_id="uuid-probe-after-leases")
        .returning(tenants.c.id)
    ) == uuid.UUID(int=before.int + 3)

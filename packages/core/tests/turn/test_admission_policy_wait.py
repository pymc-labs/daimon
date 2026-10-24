"""A turn that waited out a policy write checks its backend against what the write left.

Admission reads the tenant policy before the shared policy fence. When an admin's
write holds that fence, the turn waits (J7). The selection re-reads the policy under
the fence, so a backend switched off by that write is refused there; but admission
then checked the selected backend against its pre-wait read again, refusing a
backend the write had just allowed. `reauthorize` re-checks the policy again before
any session runs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores import access_policy
from daimon.core.turn import runtime_manifest

from .test_backend_selection import initial_backend as initial_backend
from .test_backend_selection import preference_for
from .test_backend_selection import selector_host as selector_host
from .test_provider_dispatch import TENANT


async def _writer_sets(
    db_engine, policy: TenantAccessPolicy, seconds: float, held: asyncio.Event
) -> None:
    """An admin's policy write from its OWN connection: the fixture binds every
    session to one connection, where advisory locks never conflict."""
    from daimon.testing.db import build_test_engine, isolated_test_schema
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False),
        isolated_test_schema(db_engine),
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with async_sessionmaker(engine).begin() as writer:
            await access_policy.lock_policy_writes_exclusive(writer, tenant_id=TENANT)
            held.set()
            await asyncio.sleep(seconds)
            await access_policy.set_access_policy(writer, tenant_id=TENANT, policy=policy)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_backend_allowed_while_the_turn_waited_is_admitted(
    selector_host, db_engine, db_session_factory, monkeypatch
):
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "openai", 0, actor)
    # The admin had switched OpenAI off; the turn reads that before the fence.
    async with db_session_factory.begin() as db:
        await access_policy.set_access_policy(
            db, tenant_id=TENANT, policy=TenantAccessPolicy(enabled_backends=("anthropic",))
        )

    # The fixture agent's environment and memory have no OpenAI counterpart; the
    # counterpart proof is not what this guards, so it passes here.
    async def proven(request):
        return SimpleNamespace(counterpart=None)

    monkeypatch.setattr(runtime_manifest, "read_preparation_counterpart", proven)
    held = asyncio.Event()
    # While the turn waits, the admin switches OpenAI back on.
    reopened = TenantAccessPolicy(enabled_backends=("anthropic", "openai"))
    writer = asyncio.create_task(_writer_sets(db_engine, reopened, 1.0, held))
    ready = asyncio.create_task(held.wait())
    async with asyncio.timeout(10):
        await asyncio.wait({writer, ready}, return_when=asyncio.FIRST_COMPLETED)
    if writer.done():
        writer.result()
    try:
        # Before: refused as provider_not_allowed on the pre-wait read.
        admission = await host.admit("new-thread")
    finally:
        await writer
    assert admission.backend_revision is not None
    assert admission.backend_revision.backend == "openai"

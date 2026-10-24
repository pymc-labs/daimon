"""J7 guard (smoke 0eee1b8e, Ref 8V6AW4): an admin's policy write never fails a turn.

"Write a 4000-word story" failed in four seconds with "Something went wrong on our
side": admission resolves the agent's backend under the SHARED policy fence, a
try-lock, and one miss while a parallel leg's policy write held it EXCLUSIVELY
surfaced as a raw FenceUnavailable, rendered as the generic error. The turn must
wait for the short write instead, and only past the policy deadline read as a
plain "busy, try again" (never the generic error).
"""

from __future__ import annotations

import asyncio

import pytest
from daimon.core.session_fence_retry import FenceUnavailable
from daimon.core.stores import access_policy
from daimon.core.stores.access_policy import PolicyBusyError

from .test_backend_selection import initial_backend as initial_backend
from .test_backend_selection import preference_for
from .test_backend_selection import selector_host as selector_host
from .test_provider_dispatch import TENANT


async def _writer_holds(db_engine, seconds: float, held: asyncio.Event) -> None:
    """An admin's policy write, holding the policy fence exclusively, from its OWN
    connection (advisory locks never conflict within one backend connection)."""
    from daimon.testing.db import build_test_engine, isolated_test_schema
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # db_session_factory binds every session to ONE connection, so the writer needs its
    # own engine on the same schema to contend at all.
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
    finally:
        await engine.dispose()


async def _held(writer: asyncio.Task[None], held: asyncio.Event) -> None:
    """Wait until the writer holds the fence; if it failed instead, say why at once."""
    ready = asyncio.create_task(held.wait())
    async with asyncio.timeout(10):
        await asyncio.wait({writer, ready}, return_when=asyncio.FIRST_COMPLETED)
    if writer.done():
        writer.result()


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_turn_waits_out_a_short_policy_write_instead_of_failing(selector_host, db_engine):
    # An agent backend preference sends admission through resolve_turn_backend, which
    # takes the shared policy fence first: the step that failed.
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "anthropic", 0, actor)
    held = asyncio.Event()
    writer = asyncio.create_task(_writer_holds(db_engine, 1.0, held))
    await _held(writer, held)
    try:
        # On main this raised FenceUnavailable at once; now it waits for the write.
        admission = await host.admit("new-thread")
    finally:
        await writer
    assert admission is not None


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_policy_write_past_the_deadline_reads_as_busy_not_a_raw_fence(
    selector_host, db_engine, monkeypatch
):
    # An agent backend preference sends admission through resolve_turn_backend, which
    # takes the shared policy fence first: the step that failed.
    host = selector_host
    service, agent, actor = preference_for(host)
    await service.set_backend(agent, "anthropic", 0, actor)
    monkeypatch.setattr(access_policy, "POLICY_WRITE_TIMEOUT_S", 0.3)
    held = asyncio.Event()
    writer = asyncio.create_task(_writer_holds(db_engine, 1.5, held))
    await _held(writer, held)
    try:
        with pytest.raises(PolicyBusyError):
            await host.admit("new-thread")
    finally:
        await writer


@pytest.mark.parametrize("error", [PolicyBusyError(), FenceUnavailable(0.0)])
@pytest.mark.parametrize("adapter", ["discord", "slack"])
def test_policy_contention_reads_as_busy_never_the_generic_error(adapter, error):
    import importlib

    from daimon.core.errors import TurnError

    errors = importlib.import_module(f"daimon.adapters.{adapter}.errors")
    render = errors.render_error if adapter == "discord" else errors.render_error_text
    for raised in (error, TurnError(kind="upstream", message="x", cause=error)):
        text = render(raised, request_id="01M5NVJBNMQ7SFBEF4R58V6AW4")
        assert "Something went wrong" not in text, text
        assert "busy" in text and "Try again" in text, text


@pytest.mark.parametrize("error", [PolicyBusyError(), FenceUnavailable(0.0)])
def test_teams_closes_a_busy_turn_with_the_busy_words(error):
    from daimon.adapters.teams.app import failure_notice
    from daimon.core.errors import TurnError

    for raised in (error, TurnError(kind="upstream", message="x", cause=error)):
        text = failure_notice(raised)
        assert "something went wrong" not in text.lower(), text
        assert "busy" in text and "Try again" in text, text
    # Anything else keeps Teams' existing failure words.
    assert failure_notice(RuntimeError("x")).startswith("Sorry, something went wrong")

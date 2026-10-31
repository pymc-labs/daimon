"""The awaited proof precedes publication in the existing revision namespace."""

import asyncio

import pytest
from daimon.core.channel_backend import resolve_turn_backend
from daimon.core.config import TurnSettings
from daimon.core.stores import mux_state
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.ids import ChannelRef
from sqlalchemy import text

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned


def test_native_agent_execution_defaults_on_and_stays_a_stop():
    assert TurnSettings().agent_backends is True
    assert TurnSettings(path="mux", channel_backends=True).agent_backends is True
    assert TurnSettings(agent_backends=False).agent_backends is False


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("failure", ["static", "proof", "cancelled"])
async def test_refusal_precedes_insert_and_cannot_be_read_by_old_channel_reader(
    owned, provider, failure
):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, provider, 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="slack", channel_id="proof")
    checks = []

    async def verify(selection, origin, candidate):
        checks.append(candidate)
        assert origin == "agent"
        assert selection.backend == provider
        if candidate is None and failure != "static":
            return
        if failure == "cancelled":
            raise asyncio.CancelledError
        raise AdmissionDenied(reason="backend_unsupported")

    async with sessions.begin() as session:
        with pytest.raises(asyncio.CancelledError if failure == "cancelled" else AdmissionDenied):
            await resolve_turn_backend(
                session, channel, agent, enabled=True, validate_origin=verify
            )
        assert await mux_state.latest_config_revision(session, channel) is None
        # A pre-origin-schema image reads all rows in this namespace as overrides.
        count = await session.scalar(
            text(
                "SELECT count(*) FROM channel_config_revision WHERE tenant_id=:tenant AND channel_id=:channel"
            ),
            {"tenant": agent.tenant_id, "channel": channel.channel_id},
        )
        assert count == 0
    assert checks == [None] if failure == "static" else len(checks) == 2


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_exact_candidate_is_verified_before_write_and_existing_revision_rechecks(
    owned, provider
):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, provider, 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="slack", channel_id="verified")
    checks = []

    async def verify(selection, origin, candidate):
        assert origin == "agent"
        if candidate is not None:
            checks.append(candidate)
            assert candidate.channel == channel and candidate.backend == provider
        await asyncio.sleep(0)

    async with sessions.begin() as session:
        revision, _, source = await resolve_turn_backend(
            session, channel, agent, enabled=True, validate_origin=verify
        )
        assert source == "agent" and revision is checks[0]
    async with sessions.begin() as session:
        repeated, _, _ = await resolve_turn_backend(
            session, channel, agent, enabled=True, validate_origin=verify
        )
        assert repeated == revision == checks[-1]
    assert len(checks) == 2

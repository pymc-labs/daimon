"""Card wording observes actual tickets without changing slot ownership."""

from __future__ import annotations

import asyncio
import uuid

from daimon.adapters.discord.queue_card import queue_card_ticket, tenant_slot_waiter
from daimon.core.config import DiscordSettings
from daimon.core.turn_queue import TurnQueue
from pydantic import SecretStr


async def test_real_tenant_ticket_is_scoped_and_clears_on_release() -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    queue = TurnQueue(
        platform="discord",
        global_cap=None,
        max_queued_per_tenant=5,
        max_queued=10,
        max_wait_s=30,
    )
    first = queue.admit(tenant, cap=1)
    waiting = queue.admit(tenant, cap=1)
    assert first is not None and waiting is not None
    assert first.state == "running" and waiting.state == "queued"
    assert not tenant_slot_waiter(queue, tenant)()
    with queue_card_ticket(waiting, cap=1):
        view = tenant_slot_waiter(queue, tenant)
        assert view() and not tenant_slot_waiter(queue, other)()

        async def another_task() -> None:
            # A separately admitted turn overrides its inherited task context.
            own = queue.admit(other, cap=1)
            assert own is not None
            with queue_card_ticket(own, cap=1):
                assert not tenant_slot_waiter(queue, tenant)()
                assert not tenant_slot_waiter(queue, other)()
            own.release()

        await asyncio.create_task(another_task())
        assert view() and queue.depth(tenant) == 1 and queue.in_flight(tenant) == 1
        first.release()
        assert waiting.state == "running" and not view()
        waiting.release()
    assert not tenant_slot_waiter(queue, tenant)()
    assert queue.depth() == queue.in_flight() == 0


async def test_global_only_wait_and_cancelled_ticket_are_not_tenant_cap_wording() -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    queue = TurnQueue(
        platform="discord",
        global_cap=1,
        max_queued_per_tenant=5,
        max_queued=10,
        max_wait_s=30,
    )
    first = queue.admit(other, cap=2)
    waiting = queue.admit(tenant, cap=2)
    assert first is not None and waiting is not None and waiting.queued
    with queue_card_ticket(waiting, cap=2):
        view = tenant_slot_waiter(queue, tenant)
        assert not view()
        waiting.release()
        assert not view()
    first.release()
    assert queue.depth() == queue.in_flight() == 0


def test_queue_card_flag_is_declared_default_off() -> None:
    assert DiscordSettings.model_fields["tenant_cap_waiting_card"].default is False
    assert not DiscordSettings(bot_token=SecretStr("offline")).tenant_cap_waiting_card

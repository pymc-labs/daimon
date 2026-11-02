"""M03: a conversation follows its agent's model-provider switch, or stays when shared.

`_thread_follow` decides, once admission has resolved the conversation's Anthropic
source agent, whether the person's own slot moves to what that agent now selects.
It writes nothing; `release_now` moves the slot only after the message is admitted,
and only from the generation that message read. The J1 legs
(`test_j1_switch_card_guard`) prove the whole path, several messages on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from daimon.core.turn import admission
from mux.contracts.ids import ChannelRef

TENANT = uuid.uuid4()
CHANNEL = ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="channel")
ALL = ("anthropic", "openai", "gemini")


@pytest.fixture
def prefer(monkeypatch):
    asked: list[uuid.UUID] = []

    def set_preference(provider: str | None, model: str | None = None) -> list[uuid.UUID]:
        async def current(session, tenant_id, agent_id):
            asked.append(agent_id)
            return None if provider is None else SimpleNamespace(backend=provider, model=model)

        monkeypatch.setattr(admission.channel_backend, "current_agent_backend", current)
        return asked

    return set_preference


async def follow(bound: tuple[str, str | None], *, shared=False, enabled=ALL):
    return await admission._thread_follow(  # pyright: ignore[reportPrivateUsage]
        None,  # pyright: ignore[reportArgumentType]
        tenant_id=TENANT,
        channel=CHANNEL,
        agent_ma_id="agent_daimon",
        bound_provider=bound[0],  # pyright: ignore[reportArgumentType]
        bound_model=bound[1],
        shared=shared,
        enabled_backends=enabled,  # pyright: ignore[reportArgumentType]
    )


async def test_it_asks_about_the_anthropic_source_agent(prefer):
    """Never a provider's own agent id: that one has no preference (review finding 1)."""
    from daimon.core.ma_identity import derive_agent_uuid

    asked = prefer("gemini", "gemini-3.8-flash")
    await follow(("anthropic", None))
    assert asked == [derive_agent_uuid(tenant_id=TENANT, ma_agent_id="agent_daimon")]


async def test_a_switched_agents_own_conversation_moves(prefer):
    prefer("gemini", "gemini-3.8-flash")
    assert await follow(("anthropic", None)) == admission.ThreadFollow(
        "moved", "gemini", "gemini-3.8-flash"
    )


async def test_a_shared_conversation_stays(prefer):
    prefer("gemini", "gemini-3.8-flash")
    assert await follow(("anthropic", None), shared=True) == admission.ThreadFollow(
        "stays", "gemini", "gemini-3.8-flash"
    )


@pytest.mark.parametrize(
    ("preference", "bound"),
    [
        ((None, None), ("anthropic", None)),  # no preference: the default provider
        (("gemini", "gemini-3.8-flash"), ("gemini", "gemini-3.8-flash")),
        # A provider default stored as None is that default model (review finding 5).
        (("gemini", None), ("gemini", "gemini-3.8-flash")),
        (("gemini", "gemini-3.8-flash"), ("gemini", None)),
        (("openai", None), ("openai", "gpt-6-luna")),
    ],
)
async def test_nothing_moves_when_provider_and_model_already_match(prefer, preference, bound):
    prefer(*preference)
    assert await follow(bound) is None


async def test_going_back_to_the_providers_default_model_moves(prefer):
    """Argon -> the provider default (stored as None): the slot moves to Flash."""
    prefer("gemini", None)
    moved = await follow(("gemini", "gemini-4-argon"))
    assert moved is not None and (moved.kind, moved.model) == ("moved", "gemini-3.8-flash")


async def test_never_toward_a_provider_that_cant_run_here(prefer):
    prefer("gemini", "gemini-3.8-flash")
    assert await follow(("anthropic", None), enabled=("anthropic", "openai")) is None


async def test_never_toward_a_self_hosted_model(prefer):
    from mux.profiles.selfhosted import LOOP

    prefer(LOOP.provider)
    assert await follow(("anthropic", None)) is None


async def test_release_now_moves_only_from_the_generation_it_read():
    """A slot that moved meanwhile (another message, a card) is left alone."""
    from daimon.core.thread_release import release_now
    from mux.contracts.ids import ThreadRef
    from mux.contracts.resources import ProviderBinding
    from mux.state.lease import Slot

    slot = Slot(thread=ThreadRef(channel=CHANNEL, thread_id="thread"), account_id="acct")
    current = ProviderBinding(
        id="binding",
        thread=slot.thread,
        provider="gemini",
        profile="gemini.inline_reuse",
        native_refs={"session": "s", "agent": "gemini-own-agent-id"},
        generation=3,
        config_revision=2,
        legacy_account_id="acct",
    )
    written: list[int] = []

    class Store:
        async def get_binding(self, slot, include_released=False):
            return current

        async def put_binding(self, binding, *, expected_generation):
            written.append(expected_generation)
            return binding

    moved = await release_now(
        None,  # pyright: ignore[reportArgumentType]
        Store(),  # pyright: ignore[reportArgumentType]
        slot,
        generation=2,
        tenant_id=TENANT,
        account_id=uuid.uuid4(),
        now=datetime.now(UTC),
    )
    assert moved is False and written == []

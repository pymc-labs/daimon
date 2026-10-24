"""M03: a conversation follows its agent's model-provider switch, or says it stays.

`_thread_follow` decides, before slot selection, whether the person's own slot of
this conversation moves to what its agent now selects. The J1 leg
(`test_j1_switch_card_guard`) proves the whole path on a real switch; these pin
its branches.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from daimon.core.turn import admission
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.state.lease import Slot

TENANT = uuid.uuid4()
ACCOUNT = uuid.uuid4()
CHANNEL = ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="channel")
THREAD = ThreadRef(channel=CHANNEL, thread_id="thread")
OWN = Slot(thread=THREAD, account_id=str(ACCOUNT))
SHARED = Slot(thread=THREAD)
NOW = datetime(2026, 10, 24, tzinfo=UTC)


def _binding(slot: Slot, provider: str, profile: str, revision: int = 0) -> ProviderBinding:
    return ProviderBinding(
        id=str(uuid.uuid4()),
        thread=THREAD,
        provider=provider,
        profile=profile,
        native_refs={"agent": "agent_daimon"},
        generation=1,
        config_revision=revision,
        legacy_account_id=slot.account_id,
    )


class Store:
    def __init__(self, **bindings: ProviderBinding) -> None:
        self.bindings = bindings

    async def get_binding(self, slot: Slot, include_released: bool = False):
        return self.bindings.get("own" if slot.account_id else "shared")


@pytest.fixture
def world(monkeypatch):
    released: list[Slot] = []

    async def release(session, slot, *, generation, now):
        released.append(slot)

    @contextlib.asynccontextmanager
    async def begin():
        yield None

    monkeypatch.setattr(admission, "request_thread_release", release)
    deps = SimpleNamespace(sessionmaker=SimpleNamespace(begin=begin))

    def prefer(provider: str | None, model: str | None = None) -> None:
        async def current(session, tenant_id, agent_id):
            return None if provider is None else SimpleNamespace(backend=provider, model=model)

        monkeypatch.setattr(admission.channel_backend, "current_agent_backend", current)

    async def follow(store: Store, **changes):
        values = {
            "tenant_id": TENANT,
            "channel": CHANNEL,
            "thread_id": "thread",
            "own_slot": OWN,
            "responder_ma_agent_id": None,
            "ever_shared": False,
            "eligible": True,
            "now": NOW,
        }
        values.update(changes)
        return await admission._thread_follow(None, deps, store, **values)  # pyright: ignore[reportPrivateUsage, reportArgumentType]

    return SimpleNamespace(prefer=prefer, follow=follow, released=released)


CLAUDE = ("anthropic", "anthropic.managed_agents")
GEMINI = ("gemini", "gemini.inline_reuse")


async def test_a_switched_agents_own_conversation_moves(world):
    world.prefer("gemini", "gemini-3.8-flash")
    moved = await world.follow(Store(own=_binding(OWN, *CLAUDE)))
    assert moved == admission.ThreadFollow("moved", "gemini", "gemini-3.8-flash")
    assert world.released == [OWN], "the release is requested, as a confirmed switch does"


async def test_a_shared_conversation_stays_and_says_so(world):
    world.prefer("gemini", "gemini-3.8-flash")
    store = Store(own=_binding(OWN, *CLAUDE), shared=_binding(SHARED, *CLAUDE))
    stays = await world.follow(store, ever_shared=True)
    assert stays == admission.ThreadFollow("stays", "gemini", "gemini-3.8-flash")
    assert world.released == [], "a shared conversation never moves"


async def test_nothing_moves_when_the_provider_already_matches(world):
    world.prefer(None)  # no preference: the default provider
    assert await world.follow(Store(own=_binding(OWN, *CLAUDE))) is None
    world.prefer("gemini")  # the provider's default model
    assert await world.follow(Store(own=_binding(OWN, *GEMINI))) is None
    assert world.released == []


async def test_a_model_change_off_anthropic_moves_the_conversation(world, monkeypatch):
    world.prefer("gemini", "gemini-4-argon")

    async def retained(session, channel, agent, *, enabled, retained_revision):
        return SimpleNamespace(model="gemini-3.8-flash"), None, "agent"

    monkeypatch.setattr(admission.channel_backend, "resolve_turn_backend", retained)
    moved = await world.follow(Store(own=_binding(OWN, *GEMINI, revision=3)))
    assert moved is not None and moved.kind == "moved" and moved.model == "gemini-4-argon"


async def test_a_new_conversation_has_nothing_to_move(world, monkeypatch):
    world.prefer("gemini")

    async def none(*args, **kwargs):
        return None

    monkeypatch.setattr(admission, "get_thread_session_at", none)
    assert await world.follow(Store()) is None


@pytest.mark.parametrize("changes", [{"eligible": False}, {"channel": None}, {"thread_id": None}])
async def test_a_channel_pin_a_setup_conversation_or_a_dm_never_moves(world, changes):
    """The caller passes eligible=False for a pinned channel, setup or a DM."""
    world.prefer("gemini")
    assert await world.follow(Store(own=_binding(OWN, *CLAUDE)), **changes) is None
    assert world.released == []

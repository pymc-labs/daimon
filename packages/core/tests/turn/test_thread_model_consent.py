"""One conversation on another provider: the owner's consent, the gate and the thread tier."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from daimon.core.authz import Subject
from daimon.core.channel_backend import (
    AgentBackendRefused,
    BackendActor,
    ThreadBackend,
    resolve_turn_backend,
)
from daimon.core.model_switch import SwitchRefusal
from daimon.core.stores import mux_state, security_audit
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.thread_models import (
    clear_thread_model,
    get_thread_choice,
    get_thread_consent,
    set_thread_model,
)
from daimon.core.thread_model_switch import (
    ThreadPick,
    confirm_thread_model,
    confirm_thread_or_refusal,
    pick_value,
    preview_thread_model,
    preview_thread_or_refusal,
)
from daimon.core.thread_models import thread_backend
from mux.contracts.ids import ChannelRef, ModelRef

from .test_declared_drop_service import owned as owned
from .test_declared_drop_service import switching as switching

LUNA = "openai:gpt-6-luna"
MA_AGENT = "agent_helper"


def _pick(service, agent, actor, sessions) -> ThreadPick:
    return ThreadPick(service, sessions, None, agent, actor, MA_AGENT, "discord", "person")


async def _confirm(pick: ThreadPick, value: str = LUNA, thread_id: str = "thread"):
    shown = await preview_thread_model(pick, value)
    return await confirm_thread_model(
        pick, value, thread_id=thread_id, digest=shown.digest, revision=shown.revision
    )


async def _thread(sessions, agent, *, daimon_record=False) -> ThreadBackend | None:
    async with sessions() as db:
        return await thread_backend(
            db,
            tenant_id=agent.tenant_id,
            platform="discord",
            conversation_id="thread",
            agent_id=agent.agent_id,
            daimon_record=daimon_record,
        )


async def test_a_legacy_choice_reads_as_claude_and_clearing_drops_consents(switching):
    service, agent, actor, sessions, _, _ = switching
    where = {"tenant_id": agent.tenant_id, "platform": "discord", "thread_id": "thread"}
    async with sessions.begin() as db:
        await set_thread_model(db, **where, model="claude-haiku-5-5")
        assert await get_thread_choice(db, **where) == ModelRef(
            provider="anthropic", id="claude-haiku-5-5"
        )
    await _confirm(_pick(service, agent, actor, sessions))
    async with sessions.begin() as db:
        assert await get_thread_choice(db, **where) == ModelRef(provider="openai", id="gpt-6-luna")
        assert await get_thread_consent(db, **where, agent_id=agent.agent_id) is not None
        assert await get_thread_consent(db, **where, agent_id=uuid.uuid4()) is None
        await clear_thread_model(db, **where)
    async with sessions() as db:
        assert await get_thread_consent(db, **where, agent_id=agent.agent_id) is None


async def test_a_pick_off_the_agents_provider_needs_the_owner(switching):
    service, agent, _, sessions, _, _ = switching
    stranger = BackendActor(agent.tenant_id, uuid.uuid4(), Subject())
    pick = _pick(service, agent, stranger, sessions)
    with pytest.raises(AgentBackendRefused) as refused:
        await preview_thread_model(pick, LUNA)
    assert refused.value.reason == "owner_or_admin_required"
    # A Claude pick on a Claude agent needs no consent at all.
    assert (await preview_thread_model(pick, "anthropic:claude-haiku-5-5")).preview is None
    assert await _thread(sessions, agent) is None
    async with sessions.begin() as db:
        await set_feature_value(
            db,
            tenant_id=agent.tenant_id,
            target=FeatureTarget("agent", agent_id=MA_AGENT),
            key="thread_providers",
            value="on",
            set_by=None,
            surface="chat",
        )
    change = await _confirm(pick)
    assert change.model_id == "gpt-6-luna"
    assert change.note.endswith("Earlier messages carry over.")


async def test_a_confirmed_pick_runs_this_conversation_only(switching):
    service, agent, actor, sessions, _, _ = switching
    preview = await preview_thread_model(_pick(service, agent, actor, sessions), LUNA)
    assert preview.allowed and preview.preview is not None
    assert preview.preview.declared_drops == ("sandbox",)
    await _confirm(_pick(service, agent, actor, sessions))
    thread = await _thread(sessions, agent)
    assert thread is not None and thread.consent is not None
    assert (thread.consent.backend, thread.consent.model) == ("openai", None), "its default"
    assert thread.consent.drop_consent == preview.preview.drop_consent
    assert await service.current(agent) is None, "the agent stays on Claude"
    assert await _thread(sessions, agent, daimon_record=True) == thread
    async with sessions() as db:
        audit = await security_audit.list_events(db, tenant_id=agent.tenant_id)
    assert audit[-1].tool_name == "thread_model"
    assert audit[-1].reason == "completed; model=openai:gpt-6-luna; declared_drops=sandbox"


async def test_a_stale_preview_changes_nothing(switching):
    service, agent, actor, sessions, _, _ = switching
    pick = _pick(service, agent, actor, sessions)
    with pytest.raises(AgentBackendRefused) as refused:
        await confirm_thread_model(pick, LUNA, thread_id="thread", digest="old", revision=0)
    assert refused.value.reason == "stale_preview"
    async with sessions() as db:
        where = {"tenant_id": agent.tenant_id, "platform": "discord", "thread_id": "thread"}
        assert await get_thread_choice(db, **where) is None


async def _accept(*_args, **_kwargs) -> None:
    return None


async def test_the_thread_tier_runs_its_consent_and_a_rollback_keeps_it(switching):
    service, agent, actor, sessions, _, _ = switching
    await _confirm(_pick(service, agent, actor, sessions))
    thread = await _thread(sessions, agent)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="chan")
    async with sessions.begin() as db:
        revision, _, source = await resolve_turn_backend(
            db, channel, agent, enabled=True, validate_origin=_accept, thread=thread
        )
        assert revision is not None and source == "agent"
        assert (revision.backend, revision.model) == ("openai", "gpt-6-luna")
        assert await mux_state.config_origin(db, channel, revision.local) == "thread"
        assert thread is not None and thread.consent is not None
        assert await mux_state.config_thread_pin(db, channel, revision.local) == (
            "thread",
            thread.consent.digest,
        )
        # Without the thread tier the same channel runs the agent's own Claude.
        assert (await resolve_turn_backend(db, channel, agent, enabled=True))[0] is None
    async with sessions() as db:
        retained = await resolve_turn_backend(
            db, channel, agent, enabled=True, retained_revision=revision.local
        )
    assert retained == (revision, False, "agent")


async def test_a_consent_without_its_losses_is_confirmed_again(switching):
    service, agent, actor, sessions, _, _ = switching
    await _confirm(_pick(service, agent, actor, sessions))
    thread = await _thread(sessions, agent)
    assert thread is not None and thread.consent is not None
    bare = replace(thread, consent=thread.consent.model_copy(update={"drop_consent": None}))
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="chan")
    async with sessions.begin() as db:
        with pytest.raises(AgentBackendRefused) as refused:
            await resolve_turn_backend(
                db, channel, agent, enabled=True, validate_origin=_accept, thread=bare
            )
        assert refused.value.reason == "re_confirm"
        claude = ThreadBackend("thread", None)
        assert await resolve_turn_backend(db, channel, agent, enabled=True, thread=claude) == (
            None,
            False,
            "default",
        )


def test_a_typed_word_names_its_catalog_value():
    assert pick_value(" Sonnet ") == "anthropic:claude-sonnet-5-5"
    assert pick_value(LUNA) == LUNA


async def test_refusals_come_back_as_the_words_a_chat_shows(switching):
    service, agent, _, sessions, _, _ = switching
    stranger = BackendActor(agent.tenant_id, uuid.uuid4(), Subject())
    pick = _pick(service, agent, stranger, sessions)

    refused = await preview_thread_or_refusal(pick, LUNA, admin=False)
    assert isinstance(refused, SwitchRefusal)
    assert refused.reason == "owner_or_admin_required"

    stale = await confirm_thread_or_refusal(
        _pick(service, agent, actor_of(switching), sessions),
        LUNA,
        thread_id="thread",
        digest="old",
        revision=0,
        admin=False,
    )
    assert isinstance(stale, SwitchRefusal)
    assert stale.reason == "stale_preview"
    assert await _thread(sessions, agent) is None


def actor_of(switching):
    return switching[2]

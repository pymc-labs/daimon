"""One conversation on another provider: the pick applies at once, behind the same gates."""

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
from daimon.core.stores.domain import AgentBackendRevision, FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.thread_models import (
    clear_thread_model,
    get_thread_choice,
    get_thread_consent,
    set_thread_model,
)
from daimon.core.thread_model_switch import (
    ThreadPick,
    pick_thread,
    pick_value,
    preview_thread_model,
    preview_thread_or_refusal,
)
from daimon.core.thread_models import ThreadModelChange, thread_backend
from mux.contracts.ids import ChannelRef, ModelRef

from .test_declared_drop_service import owned as owned
from .test_declared_drop_service import switching as switching

LUNA = "openai:gpt-6-luna"
MA_AGENT = "agent_helper"


def _pick(service, agent, actor, sessions) -> ThreadPick:
    return ThreadPick(service, sessions, None, agent, actor, MA_AGENT, "discord", "person")


async def _keep(pick: ThreadPick, value: str = LUNA) -> ThreadModelChange:
    done = await pick_thread(pick, value, thread_id="thread", admin=False)
    assert isinstance(done, ThreadModelChange), f"the pick is kept: {done}"
    return done


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


async def _accept(*_args, **_kwargs) -> None:
    return None


def _channel(agent) -> ChannelRef:
    return ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="chan")


async def test_a_legacy_choice_reads_as_claude_and_clearing_drops_the_run(switching):
    """An old Claude-only row reads as Claude; a pick stores the agent's run; clear removes it."""
    service, agent, actor, sessions, _, _ = switching
    where = {"tenant_id": agent.tenant_id, "platform": "discord", "thread_id": "thread"}
    async with sessions.begin() as db:
        await set_thread_model(db, **where, model="claude-haiku-5-5")
        assert await get_thread_choice(db, **where) == ModelRef(
            provider="anthropic", id="claude-haiku-5-5"
        ), "a legacy row is a Claude model"
    await _keep(_pick(service, agent, actor, sessions))
    async with sessions.begin() as db:
        assert await get_thread_choice(db, **where) == ModelRef(
            provider="openai", id="gpt-6-luna"
        ), "the pick is stored"
        assert await get_thread_consent(db, **where, agent_id=agent.agent_id) is not None, (
            "the agent's run for this conversation is stored"
        )
        assert await get_thread_consent(db, **where, agent_id=uuid.uuid4()) is None, (
            "another agent has none"
        )
        await clear_thread_model(db, **where)
    async with sessions() as db:
        assert await get_thread_consent(db, **where, agent_id=agent.agent_id) is None, "cleared"


async def test_a_pick_off_the_agents_provider_needs_the_owner(switching):
    """Who may switch stays: a stranger is refused until the agent lets anyone pick."""
    service, agent, _, sessions, _, _ = switching
    stranger = BackendActor(agent.tenant_id, uuid.uuid4(), Subject())
    pick = _pick(service, agent, stranger, sessions)
    with pytest.raises(AgentBackendRefused) as refused:
        await preview_thread_model(pick, LUNA)
    assert refused.value.reason == "owner_or_admin_required", refused.value.reason
    assert (await preview_thread_model(pick, "anthropic:claude-haiku-5-5")).preview is None, (
        "a Claude pick on a Claude agent has nothing to check"
    )
    assert await _thread(sessions, agent) is None, "nothing was kept"
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
    change = await _keep(pick)
    assert change.model_id == "gpt-6-luna", "the pick applies"
    assert "Earlier messages carry over." in change.note, change.note


async def test_a_lossy_pick_is_kept_at_once_and_runs_this_conversation_only(switching):
    """Sandbox is off on OpenAI: said as information, kept in one call, audited."""
    service, agent, actor, sessions, _, reads = switching
    change = await _keep(_pick(service, agent, actor, sessions))
    assert reads == [agent], "one source read: the stored run's preview is the one shown"
    assert "Off on OpenAI: sandbox." in change.note, change.note
    assert "?" not in change.note, "information, never a question"
    thread = await _thread(sessions, agent)
    assert thread is not None and thread.consent is not None, "the conversation's run is stored"
    assert (thread.consent.backend, thread.consent.model) == ("openai", None), "its default"
    assert await service.current(agent) is None, "the agent stays on Claude"
    assert await _thread(sessions, agent, daimon_record=True) == thread, "both records agree"
    async with sessions() as db:
        audit = await security_audit.list_events(db, tenant_id=agent.tenant_id)
    assert audit[-1].tool_name == "thread_model", "the pick is audited"
    assert audit[-1].reason == "completed; model=openai:gpt-6-luna; declared_drops=sandbox", audit[
        -1
    ].reason


async def test_the_thread_tier_runs_its_revision_and_a_rollback_keeps_it(switching):
    """The conversation's run outranks the agent's; a retained revision resolves the same."""
    service, agent, actor, sessions, _, _ = switching
    await _keep(_pick(service, agent, actor, sessions))
    thread = await _thread(sessions, agent)
    channel = _channel(agent)
    async with sessions.begin() as db:
        revision, _, source = await resolve_turn_backend(
            db, channel, agent, enabled=True, validate_origin=_accept, thread=thread
        )
        assert revision is not None and source == "agent", "the thread's run is used"
        assert (revision.backend, revision.model) == ("openai", "gpt-6-luna"), "on its model"
        assert await mux_state.config_origin(db, channel, revision.local) == "thread", "origin"
        assert thread is not None and thread.consent is not None
        assert await mux_state.config_thread_pin(db, channel, revision.local) == (
            "thread",
            thread.consent.digest,
        ), "pinned to the thread"
        assert (await resolve_turn_backend(db, channel, agent, enabled=True))[0] is None, (
            "without the thread tier the channel runs the agent's own Claude"
        )
    async with sessions() as db:
        retained = await resolve_turn_backend(
            db, channel, agent, enabled=True, retained_revision=revision.local
        )
    assert retained == (revision, False, "agent"), "a rollback keeps the thread's run"


@pytest.mark.parametrize(
    "legacy",
    [
        None,
        {"declared_drops": ["sandbox"], "source_pin": "a" * 64, "target_digest": "b" * 64},
        {"declared_drops": ["memory"], "source_pin": "c" * 64, "target_digest": "d" * 64},
    ],
)
async def test_a_stored_run_runs_whatever_its_old_consent_said(switching, legacy):
    """Old rows parse, and their consent is ignored: no row is ever asked to confirm again."""
    service, agent, actor, sessions, _, _ = switching
    await _keep(_pick(service, agent, actor, sessions))
    thread = await _thread(sessions, agent)
    assert thread is not None and thread.consent is not None
    stored = thread.consent.model_dump(mode="json") | {"drop_consent": legacy}
    old = replace(thread, consent=AgentBackendRevision.model_validate(stored))
    assert "drop_consent" not in old.consent.model_dump(), "never written back"
    async with sessions.begin() as db:
        revision, _, _ = await resolve_turn_backend(
            db, _channel(agent), agent, enabled=True, validate_origin=_accept, thread=old
        )
        assert revision is not None, "the old row runs"
        assert (revision.backend, revision.model) == ("openai", "gpt-6-luna"), "on its model"
        claude = ThreadBackend("thread", None)
        assert await resolve_turn_backend(
            db, _channel(agent), agent, enabled=True, thread=claude
        ) == (None, False, "default"), "a Claude conversation runs the agent's own"


async def test_a_claude_pick_is_kept_at_once_too(switching):
    """A pick that loses nothing is kept the same way, with nothing to say about losses."""
    service, agent, actor, sessions, _, _ = switching
    change = await _keep(_pick(service, agent, actor, sessions), "anthropic:claude-haiku-5-5")
    assert "Off on" not in change.note, change.note
    async with sessions() as db:
        choice = await get_thread_choice(
            db, tenant_id=agent.tenant_id, platform="discord", thread_id="thread"
        )
    assert choice == ModelRef(provider="anthropic", id="claude-haiku-5-5"), "kept at once"


def test_a_typed_word_names_its_catalog_value():
    """A typed family name maps to its catalog value; a catalog value is kept."""
    assert pick_value(" Sonnet ") == "anthropic:claude-sonnet-5-5", "the family's value"
    assert pick_value(LUNA) == LUNA, "a value stays"


async def test_refusals_come_back_as_the_words_a_chat_shows(switching):
    """A refused pick is the shared words and keeps nothing."""
    service, agent, _, sessions, _, _ = switching
    stranger = BackendActor(agent.tenant_id, uuid.uuid4(), Subject())
    pick = _pick(service, agent, stranger, sessions)

    refused = await preview_thread_or_refusal(pick, LUNA, admin=False)
    assert isinstance(refused, SwitchRefusal), "a refusal, not an exception"
    assert refused.reason == "owner_or_admin_required", refused.reason
    kept = await pick_thread(pick, LUNA, thread_id="thread", admin=False)
    assert isinstance(kept, SwitchRefusal), "the pick itself is refused the same way"
    assert await _thread(sessions, agent) is None, "nothing was kept"

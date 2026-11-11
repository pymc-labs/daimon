"""Admission asks again, off the turn, when a conversation's own model consent went stale.

An agent edit leaves the stored consent for a conversation's pick stale. The message runs on
the agent's own model and the card is posted again; the agent is not told to offer an agent
switch, which is the wrong card for a conversation's pick. A conversation without a pick
(an agent's own model) is untouched.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from daimon.core import thread_model_reask
from daimon.core.channel_backend import AgentBackendRefused, ThreadBackend
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.domain import AgentBackendRevision
from daimon.core.thread_model_reask import StaleThread
from daimon.core.turn import admission, runtime_manifest
from daimon.core.turn.errors import AdmissionDenied

from .test_backend_selection import PROFILE, TENANT
from .test_backend_selection import initial_backend as initial_backend
from .test_backend_selection import selector_host as selector_host

HEX = "a" * 64


def _consent() -> AgentBackendRevision:
    """What `Confirm` stored for the conversation: openai's default model, losses accepted."""
    return AgentBackendRevision.model_validate(
        {
            "tenant_id": TENANT,
            "agent_id": derive_agent_uuid(tenant_id=TENANT, ma_agent_id="ag_parity_test"),
            "local": 1,
            "backend": "openai",
            "model": "gpt-6-luna",
            "profile": PROFILE,
            "agent_name": "test-agent",
            "preview_digest": HEX,
            "digest": HEX,
            "previous_local": None,
            "created_at": datetime.now(UTC),
            "drop_consent": {"declared_drops": [], "source_pin": HEX, "target_digest": HEX},
        }
    )


@pytest.fixture
def asked(selector_host, monkeypatch):
    """A conversation with a stored consent that refuses `re_confirm`; the asks, as made."""
    consent = _consent()

    async def thread_of(*_, conversation_id, **__):
        return ThreadBackend(conversation_id, consent) if conversation_id == "thread" else None

    async def stale(request):
        raise AgentBackendRefused("re_confirm")

    calls: list[StaleThread] = []
    monkeypatch.setattr(admission, "thread_backend", thread_of)
    monkeypatch.setattr(runtime_manifest, "prepare_source", stale)
    monkeypatch.setattr(
        thread_model_reask, "spawn_thread_reask", lambda **kw: calls.append(kw["stale"])
    )

    async def notify(card):
        return None

    # The deps are frozen and shared with `host.admit`; the notifier is wired as an adapter does.
    object.__setattr__(selector_host.deps, "thread_reask_notifier", notify)
    return SimpleNamespace(calls=calls, consent=consent, host=selector_host)


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_stale_conversation_consent_runs_on_the_agents_model_and_asks_again(asked):
    prepared = await asked.host.prepare("thread")

    admitted = prepared.admission
    assert admitted.backend_revision is None and admitted.backend is None, "the agent's own model"
    assert admitted.backend_switch_stale is None, "the agent is not told to offer a switch"
    assert prepared.session_ref is None, "nothing ran on the stale provider"
    assert len(asked.calls) == 1
    call = asked.calls[0]
    assert (call.conversation_id, call.channel_id, call.platform) == ("thread", "channel", "slack")
    assert call.consent == asked.consent and call.user_id == "caller"


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_deployment_without_the_notifier_keeps_the_agent_switch_notice(asked):
    object.__setattr__(asked.host.deps, "thread_reask_notifier", None)

    prepared = await asked.host.prepare("thread")

    assert prepared.admission.backend_switch_stale == "openai"
    assert asked.calls == []


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_a_conversation_on_the_agents_own_model_is_unchanged(asked):
    prepared = await asked.host.prepare("other-thread")

    assert prepared.admission.backend_revision is None
    assert prepared.admission.backend_switch_stale is None
    assert asked.calls == [], "no pick, no card"


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_an_unattended_turn_is_refused_and_asks_nothing(asked):
    with pytest.raises(AdmissionDenied) as refused:
        await asked.host.admit("thread", origin="routine", attended=False)

    assert refused.value.cause == "re_confirm"
    assert asked.calls == []

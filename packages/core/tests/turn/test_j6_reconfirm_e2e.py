"""J6 (g) offline E2E: a copy whose stored switch consent went stale is asked to confirm
again, never told its provider isn't set up.

SMOKE 42591c55 J6g (dsh diagnosis 5): a Gemini Flash copy answered "This agent runs on
Gemini, which isn't set up here yet" while Gemini was set up. The preview's stored
consent no longer matched the source the turn captured (`prepare_source` refuses
`re_confirm`), and that refusal reached admission as a causeless `backend_unsupported`,
which reads as missing provider setup. The person could not recover from it.

Now the turn runs on the agent's source (Anthropic) and the agent is told its switch
needs confirming again, so it can offer a fresh card. A conversation already on the
provider still gets the card, and a channel pin is never bypassed.
"""

import pytest
from daimon.core.stores import mux_state, thread_provider_releases
from daimon.core.turn.agent_notices import stale_switch_notice
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from mux.contracts.config import BackendConfig

from .test_backend_selection import (
    PROFILE,
    REVISION,
    preference_for,
)
from .test_backend_selection import (
    initial_backend as initial_backend,
)
from .test_backend_selection import (
    selector_host as selector_host,
)

NOUNS = RefusalNouns(scope="server", admin="a server admin", billing="`/billing`")


async def _stale(host, monkeypatch, provider):
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.turn import runtime_manifest

    service, agent, actor = preference_for(host)
    # The copy was switched (to Gemini Flash in the smoke); its consent is now stale.
    await service.set_backend(agent, provider, 0, actor)
    checks: list[object] = []

    async def stale(request):
        checks.append(request)
        raise AgentBackendRefused("re_confirm")

    monkeypatch.setattr(runtime_manifest, "prepare_source", stale)
    return checks


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("provider", ["gemini", "openai"])
async def test_j6_a_stale_switch_runs_on_its_source_and_tells_the_agent(
    selector_host, monkeypatch, provider
):
    host = selector_host
    checks = await _stale(host, monkeypatch, provider)
    async with host.sessions() as db:
        before = await mux_state.latest_config_revision_and_sharing(db, REVISION.channel)
        next_local = await mux_state.next_config_local(db, REVISION.channel)

    first = await host.prepare("thread")
    admitted = first.admission
    assert admitted.backend_revision is None and admitted.backend is None
    assert admitted.backend_revision_origin is None
    assert admitted.backend_switch_stale == provider
    assert admitted.backend_footer_profile == "anthropic.managed_agents"
    assert first.session_ref is None and host.created == [first.ma_session_id]
    assert host.wire.requests == []  # nothing ran, or was metered, on the provider
    binding = await host.store.get_binding(host.slot)
    assert binding is not None and binding.provider == "anthropic"
    assert binding.config_revision == 0

    # Nothing sticks: the next message checks the switch again in the same session.
    again = await host.prepare("thread")
    assert again.ma_session_id == first.ma_session_id and again.reused
    assert again.admission.backend_switch_stale == provider
    assert len(checks) == 2
    assert await host.store.get_binding(host.slot) == binding
    async with host.sessions() as db:
        assert await thread_provider_releases.requested_generation(db, host.slot) is None
        assert await mux_state.latest_config_revision_and_sharing(db, REVISION.channel) == before
        assert await mux_state.next_config_local(db, REVISION.channel) == next_local


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_j6_a_channel_pin_is_never_bypassed_by_a_stale_switch(selector_host, monkeypatch):
    host = selector_host
    checks = await _stale(host, monkeypatch, "gemini")
    pin = await host.change(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"))
    prepared = await host.prepare("thread")
    assert prepared.admission.backend_revision == pin
    assert prepared.admission.backend_revision_origin == "channel"
    assert prepared.admission.backend_switch_stale is None
    assert prepared.session_ref is not None and prepared.session_ref.provider == "openai"
    assert checks == []


@pytest.mark.parametrize(("provider", "named"), [("gemini", "Gemini"), ("openai", "OpenAI")])
def test_j6_the_agent_hears_which_switch_to_offer_again(provider, named):
    said = stale_switch_notice(provider)
    assert said.startswith(f"Your switch to {named} needs to be confirmed again")
    assert "request_agent_backend_switch" in said


def test_j6_a_conversation_already_on_the_provider_still_asks_for_a_fresh_card():
    said = admission_refusal_text(
        "backend_unsupported",
        NOUNS,
        agent_name="owner-flash",
        cause="re_confirm",
        provider="gemini",
    )
    assert said == (
        "owner-flash's model-provider switch needs to be confirmed again.\n\n"
        "In a new conversation, ask owner-flash to switch to Gemini again, then Confirm."
    )
    assert "isn't set up" not in said

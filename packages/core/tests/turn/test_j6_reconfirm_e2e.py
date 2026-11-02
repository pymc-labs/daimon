"""J6 (g) offline E2E: a copy whose stored switch consent went stale is asked to confirm
again, never told its provider isn't set up.

SMOKE 42591c55 J6g (dsh diagnosis 5): a Gemini Flash copy answered "This agent runs on
Gemini, which isn't set up here yet" while Gemini was set up. The preview's stored
consent no longer matched the source the turn captured (`prepare_source` refuses
`re_confirm`), and that refusal reached admission as a causeless `backend_unsupported`,
which reads as missing provider setup. The person could not recover from it.
"""

from datetime import UTC, datetime

import pytest
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn_hold import RequesterOrg

from .test_backend_selection import (
    initial_backend as initial_backend,
)
from .test_backend_selection import (
    preference_for,
)
from .test_backend_selection import (
    selector_host as selector_host,
)
from .test_provider_dispatch import TENANT

NOUNS = RefusalNouns(scope="server", admin="a server admin", billing="`/billing`")


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize(("provider", "named"), [("gemini", "Gemini"), ("openai", "OpenAI")])
async def test_j6_a_stale_switch_consent_asks_for_a_fresh_card(
    selector_host, monkeypatch, provider, named
):
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.turn import runtime_manifest

    host = selector_host
    service, agent, actor = preference_for(host)
    # The copy was switched (to Gemini Flash in the smoke); its consent is now stale.
    await service.set_backend(agent, provider, 0, actor)

    async def stale(_request):
        raise AgentBackendRefused("re_confirm")

    monkeypatch.setattr(runtime_manifest, "prepare_source", stale)
    with pytest.raises(AdmissionDenied, match="backend_unsupported") as refused:
        await admit_impl(
            host.deps,
            tenant_id=TENANT,
            requester_org=RequesterOrg.HOST,
            platform="slack",
            external_user_id="caller",
            channel_id="channel",
            thread_id="new",
            now=datetime.now(UTC),
            origin="chat",
        )
    assert (refused.value.cause, refused.value.provider) == ("re_confirm", provider)
    said = admission_refusal_text(
        "backend_unsupported",
        NOUNS,
        agent_name="owner-flash",
        cause=refused.value.cause,
        provider=refused.value.provider,
    )
    assert said == (
        "owner-flash's model-provider switch needs to be confirmed again.\n\n"
        f"Ask again to preview the switch to {named}, then Confirm on the new card."
    )
    assert "isn't set up" not in said
    assert host.wire.requests == [] and host.created == []

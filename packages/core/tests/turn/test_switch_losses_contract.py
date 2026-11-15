"""One agent, a new model: every surface names exactly what a real turn loses.

An agent with Daimon MCP (memory and web_fetch included), a custom connector and custom
skills switches to Gemini and to OpenAI, relay on and off. The preview's lines,
the card's words, the stored consent and preparation's declared drops are one
reading; a channel-less read of the agent (any copy's) and an admin's read agree.
"""

from __future__ import annotations

import pytest
from daimon.core.host_web_fetch import PINNED_HOST_TOOLS

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_gemini_template_mcp import CONNECTOR_PARAMS as GEMINI_CONNECTOR
from .test_gemini_template_mcp import template as gemini_template  # noqa: F401
from .test_openai_template_mcp import CONNECTOR_PARAMS as OPENAI_CONNECTOR
from .test_openai_template_mcp import handoff_mcp
from .test_openai_template_mcp import template as openai_template  # noqa: F401

#: Every Daimon MCP tool on, as an agent's default toolset has it.
OPENAI_AGENT = {
    **OPENAI_CONNECTOR,
    "tools": [
        OPENAI_CONNECTOR["tools"][0],
        GEMINI_CONNECTOR["tools"][1],
        *OPENAI_CONNECTOR["tools"][2:],
    ],
}

#: Whole categories a real OpenAI or Gemini turn keeps: never a loss here.
_KEPT = {"mcp", "memory", "skills", "tools", "sandbox", "credentials"}


@pytest.fixture
async def source_mcp(consent_request):
    async with handoff_mcp(consent_request[0], PINNED_HOST_TOOLS) as app:
        yield app


async def _one_reading(request, quote, consent_request, *, relay: bool):
    from daimon.core.channel_backend import switch_losses
    from daimon.core.confirmation import drop_words
    from daimon.core.stores.accounts import set_role
    from daimon.core.stores.domain import Role
    from daimon.core.turn.admission import capture_agent_source
    from daimon.core.turn.runtime_manifest import prepare_source, preview_source_drops

    drops = quote.declared_drops
    assert not _KEPT & set(drops), drops
    assert ("mcp_server:linear" in drops) is not relay
    assert not [d for d in drops if d.startswith("mcp_tool:")], drops
    assert quote.unsupported_mcps == (() if relay else ("linear",))
    # A skill past the provider's size limits is the only skill named, each by itself.
    skills = tuple(sorted(d.removeprefix("skill:") for d in drops if d.startswith("skill:")))
    assert quote.unsupported_skills == skills
    assert (quote.unsupported_skills, quote.unsupported_mcps, quote.unsupported_capabilities) == (
        switch_losses(drops, consent_request[2])
    )
    if drops:
        assert "connected tools" not in drop_words(drops)
    assert quote.drop_consent is not None and quote.drop_consent.declared_drops == drops
    prepared = await prepare_source(request)
    assert prepared.target_spec is not None and prepared.target_spec.declared_drops == drops
    # A copy is read as its own agent, with no channel: the same losses, admin or not.
    _, _, source, actor, *_ = consent_request
    model = prepared.target_spec.model
    for role in (Role.USER, Role.ADMIN):
        async with request.deps.sessionmaker.begin() as db:
            await set_role(db, request.admission.account_id, role)
        captured = await capture_agent_source(
            request.deps,
            source,
            account_id=request.admission.account_id,
            subject=actor.subject,
            external_user_id=request.external_user_id,
        )
        assert preview_source_drops(captured, model).declared_drops == drops, role


@pytest.mark.parametrize("consent_request", [GEMINI_CONNECTOR], indirect=True)
@pytest.mark.parametrize("gemini_template", [None, "relay"], indirect=True)
async def test_gemini_names_only_what_its_turn_loses(gemini_template, consent_request):  # noqa: F811
    request, quote, *_ = gemini_template
    relay = "mcp_server:linear" not in quote.declared_drops
    await _one_reading(request, quote, consent_request, relay=relay)
    # Grounding is off: web search is a named loss; Daimon's own web_fetch carries fetch.
    assert "builtin_tool:web_search" in quote.declared_drops
    assert "builtin_tool:web_fetch" not in quote.declared_drops


@pytest.mark.parametrize("consent_request", [OPENAI_AGENT], indirect=True)
@pytest.mark.parametrize("openai_template", [{}, {"relay": True}], indirect=True)
async def test_openai_names_only_what_its_turn_loses(openai_template, consent_request):  # noqa: F811
    request, quote, *_ = openai_template
    relay = "mcp_server:linear" not in quote.declared_drops
    await _one_reading(request, quote, consent_request, relay=relay)

"""The model preview and the consent an apply carries, over the web routes."""

from __future__ import annotations

import httpx
from daimon.core.channel_backend import NO_CAPTURE, make_backend_service
from daimon.core.models_catalog import list_model_choices
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN as _OWN
from .agent_support import Agents, consent_body, provider_choice
from .agent_support import agent_client as _client
from .agent_support import agent_people as _people
from .agent_support import make_admin as _admin
from .agent_support import sign_in_token as _token

_OPENAI = "openai:gpt-6-astra"


async def _moved_draft(client: httpx.AsyncClient, agents: Agents) -> None:
    """Draft the agent onto the OpenAI model."""
    config = (await client.get("/api/agents/ag_w")).json()
    config["model"] = {**config["model"], "id": "gpt-6-astra", "backend": "openai"}
    draft = await client.patch(
        "/api/agents/ag_w/draft", json={"base_version": 3, "config": config}, headers=_OWN
    )
    assert draft.status_code == 200, draft.text
    assert agents.agent.version == 3, "a draft changes nothing yet"


async def test_the_web_catalog_is_the_core_catalog(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Every model the editor lists is one core offers this workspace, and no other."""
    agents, owner, _other = await _people(db_session)
    await _admin(db_session_factory, owner)
    settings = await provider_choice(db_session_factory, owner, ("anthropic", "openai"))
    policy = await make_backend_service(
        db_session_factory, settings.turn, capture_source=NO_CAPTURE
    ).tenant_policy(owner.tenant_id)
    token = await _token(db_session_factory, owner)
    async with _client(
        agents, owner.tenant_id, db_session_factory, token, settings=settings
    ) as client:
        web = (await client.get("/api/agents/catalog")).json()["models"]
    core = list_model_choices(tenant=policy, default=None)
    assert {(m["backend"], m["id"]) for m in web} == {(c.provider, c.id) for c in core}, (
        "the web catalog is core's catalog"
    )


async def test_a_provider_apply_needs_the_consent_its_preview_asks_for(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No consent and a stale digest both leave the agent as it was; the shown one lands."""
    agents, owner, _other = await _people(db_session)
    await _admin(db_session_factory, owner)
    settings = await provider_choice(db_session_factory, owner, ("anthropic", "openai"))
    token = await _token(db_session_factory, owner)
    async with _client(
        agents, owner.tenant_id, db_session_factory, token, settings=settings, with_turns=True
    ) as client:
        preview = (await client.get(f"/api/agents/ag_w/model-preview?value={_OPENAI}")).json()
        await _moved_draft(client, agents)
        bare = await client.post("/api/agents/ag_w/apply", json={}, headers=_OWN)
        stale = await client.post(
            "/api/agents/ag_w/apply",
            json={**await consent_body(client), "preview_digest": "0" * 64},
            headers=_OWN,
        )
        shown = await client.post(
            "/api/agents/ag_w/apply", json=await consent_body(client), headers=_OWN
        )
        after = (await client.get("/api/agents/ag_w")).json()["model"]
    assert preview["moves"] and preview["allowed"], "the preview says the move is allowed"
    assert preview["drops"], "the preview lists what the other provider drops"
    assert bare.status_code == 422, "drops not accepted refuse the apply"
    assert stale.status_code == 409, "a preview that moved since refuses the apply"
    assert shown.status_code == 200, shown.text
    assert (after["backend"], after["id"]) == ("openai", "gpt-6-astra"), "the shown one landed"


async def test_a_preview_of_a_model_not_offered_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A provider the workspace has not allowed has no preview, and says who can help."""
    agents, owner, _other = await _people(db_session)
    await _admin(db_session_factory, owner)
    settings = await provider_choice(db_session_factory, owner, ("anthropic",))
    token = await _token(db_session_factory, owner)
    async with _client(
        agents, owner.tenant_id, db_session_factory, token, settings=settings, with_turns=True
    ) as client:
        shown = await client.get(f"/api/agents/ag_w/model-preview?value={_OPENAI}")
    body = shown.json()
    assert shown.status_code == 200, shown.text
    assert body["refusal"] is not None and body["allowed"] is False, "the preview is a refusal"

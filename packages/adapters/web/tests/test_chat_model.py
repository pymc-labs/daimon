"""The model a chat header shows, and the refusal card's way back to Claude."""

from __future__ import annotations

import httpx
from daimon.adapters.web.chat_model import model_on
from daimon.adapters.web.refusals import backend_refusal
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import AgentBackendRevision
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN as _OWN
from .agent_support import agent_client as _client
from .agent_support import agent_people as _people
from .agent_support import make_admin as _admin
from .agent_support import provider_choice
from .agent_support import sign_in_token as _token


def _denied(cause: str | None) -> AdmissionDenied:
    return AdmissionDenied(
        reason="backend_unsupported",
        cause=cause,  # pyright: ignore[reportArgumentType]
        provider="openai",
    )


def test_the_header_model_follows_the_provider_the_chat_is_on() -> None:
    """Claude shows the agent's own model; another provider shows what the agent last
    picked there, else that provider's default."""
    agent = ma_agent(model="claude-sonnet-4-6")
    picked = AgentBackendRevision.model_construct(backend="openai", model="gpt-6-astra")
    assert model_on(agent, "anthropic", picked) == "claude-sonnet-4-6", "Claude is the record"
    assert model_on(agent, "openai", picked) == "gpt-6-astra", "the model picked on that provider"
    assert model_on(agent, "openai", None) != "claude-sonnet-4-6", "never a Claude id elsewhere"


def test_a_refusal_card_offers_the_way_back_only_to_an_admin() -> None:
    """An admin gets the Switch back action; anyone else is told who can help."""
    for cause in ("channel_pin", "provider_not_allowed", "provider_not_configured", None):
        admin = backend_refusal(_denied(cause), agent="Scout", admin=True)
        member = backend_refusal(_denied(cause), agent="Scout", admin=False)
        assert admin.action == "switch_back_to_claude", f"{cause}: an admin can switch back"
        assert member.action is None, f"{cause}: a member has no button"
        assert member.help == "Ask an admin.", f"{cause}: a member is sent to an admin"


def test_a_switch_that_needs_confirming_again_is_wording_only() -> None:
    """The web has no confirm card to raise, so it names who can apply the switch again."""
    card = backend_refusal(_denied("re_confirm"), agent="Scout", admin=True)
    assert card.action is None, "no button for a card the web cannot show"
    assert "again" in card.help, "the help says to apply it again"


async def test_the_way_back_answers_with_a_line_or_the_words_that_refuse(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An admin's button on a chat answers with a line; a member's is refused in words."""
    agents, owner, member = await _people(db_session)
    await _admin(db_session_factory, owner)
    settings = await provider_choice(db_session_factory, owner, ("anthropic", "openai"))
    async with db_session_factory.begin() as db:
        threads = [
            await chat_store.create_thread(
                db,
                tenant_id=person.tenant_id,
                person_id=person.id,
                agent_id="ag_w",
                agent_name="helper",
                title="New chat",
            )
            for person in (owner, member)
        ]
    answers: list[httpx.Response] = []
    for person, thread in zip((owner, member), threads, strict=True):
        token = await _token(db_session_factory, person)
        async with _client(
            agents, owner.tenant_id, db_session_factory, token, settings=settings, with_turns=True
        ) as client:
            answers.append(
                await client.post(f"/api/chat/sessions/{thread.id}/switch-back", headers=_OWN)
            )
    admin_answer, member_answer = answers
    assert admin_answer.status_code == 200 and admin_answer.json()["line"], admin_answer.text
    assert member_answer.status_code == 403, member_answer.text
    assert "admin" in member_answer.json()["detail"].lower(), "the refusal names who can help"


async def test_a_chat_keeps_its_own_model_and_a_member_cannot_move_it_off_the_provider(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A Claude pick on a Claude agent is kept at once and shows in the header; a pick on
    another provider is refused in words to someone who neither owns the agent nor is an admin."""
    agents, owner, member = await _people(db_session)
    await _admin(db_session_factory, owner)
    settings = await provider_choice(db_session_factory, owner, ("anthropic", "openai"))
    async with db_session_factory.begin() as db:
        threads = [
            await chat_store.create_thread(
                db,
                tenant_id=person.tenant_id,
                person_id=person.id,
                agent_id="ag_w",
                agent_name="helper",
                title="New chat",
            )
            for person in (owner, member)
        ]
    haiku, luna = "anthropic:claude-haiku-5-5", "openai:gpt-6-luna"
    token = await _token(db_session_factory, owner)
    async with _client(
        agents, owner.tenant_id, db_session_factory, token, settings=settings, with_turns=True
    ) as client:
        base = f"/api/chat/sessions/{threads[0].id}"
        offered = (await client.get(f"{base}/models", headers=_OWN)).json()
        assert haiku in {c["value"] for c in offered["choices"]}, offered
        assert offered["current"] is None
        shown = (await client.get(f"{base}/model-preview?value={haiku}", headers=_OWN)).json()
        assert shown["moves"] is False and shown["refusal"] is None, shown
        kept = await client.put(base + "/model", json={"value": haiku}, headers=_OWN)
        assert kept.status_code == 200, kept.text
        assert (await client.get(f"{base}/models", headers=_OWN)).json()["current"] == haiku
        await client.get("/api/chat/agents", headers=_OWN)  # the roster the header reads
        header = (await client.get(base, headers=_OWN)).json()["session"]
        assert header["agent"]["model"] == "claude-haiku-5-5", header

    token = await _token(db_session_factory, member)
    async with _client(
        agents, owner.tenant_id, db_session_factory, token, settings=settings, with_turns=True
    ) as client:
        base = f"/api/chat/sessions/{threads[1].id}"
        refused = (await client.get(f"{base}/model-preview?value={luna}", headers=_OWN)).json()
        assert refused["refusal"] is not None and refused["allowed"] is False, refused
        put = await client.put(base + "/model", json={"value": luna}, headers=_OWN)
        assert put.status_code == 403, put.text

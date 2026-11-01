"""`/api/agents`: what each person reads, and a draft from edit to apply."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import Fernet
from daimon.adapters.web.app import create_app
from daimon.adapters.web.cookies import new_session_token, token_sha256
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_skill_files
from daimon.core.authz import build_subject
from daimon.core.channel_backend import BackendActor, make_backend_service
from daimon.core.config import Settings, TurnSettings
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_mcp_credentials as credentials_store
from daimon.core.stores import agent_owners, platform_names
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_account, make_tenant, make_web_person
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, json_body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import PUBLIC_URL, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
NOW = datetime(2030, 1, 15, 12, tzinfo=UTC)


class _Agents:
    """One agent in MA whose version moves with each update."""

    def __init__(self, agent: BetaManagedAgentsAgent) -> None:
        self.agent = agent
        self.updates: list[dict[str, Any]] = []
        self.router = MARouter()
        self.router.add("GET", r"/v1/agents$", lambda _r, _m: self._list())
        self.router.add("GET", r"/v1/agents/ag_w$", lambda _r, _m: self._json())
        self.router.add("POST", r"/v1/agents/ag_w$", self._update)
        self.skills: list[dict[str, Any]] = []
        self.router.add(
            "GET",
            r"/v1/skills$",
            lambda _r, _m: httpx.Response(
                200, json={"data": self.skills, "has_more": False, "next_page": None}
            ),
        )

    def _json(self) -> httpx.Response:
        return httpx.Response(200, json=self.agent.model_dump(mode="json"))

    def _list(self) -> httpx.Response:
        body = {"data": [self.agent.model_dump(mode="json")], "has_more": False}
        return httpx.Response(200, json=body)

    def _update(self, req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        self.updates.append(body)
        fields = {k: body[k] for k in ("description", "system") if k in body}
        self.agent = self.agent.model_copy(update={"version": self.agent.version + 1, **fields})
        return self._json()


async def _people(
    session: AsyncSession, **agent_fields: Any
) -> tuple[_Agents, WebPersonRow, WebPersonRow]:
    """An agent owned by one member, and another member of the same workspace."""
    tenant = await make_tenant(session)
    owner = await make_web_person(
        session, tenant=tenant, account=await make_account(session, tenant=tenant)
    )
    other = await make_web_person(
        session,
        tenant=tenant,
        account=await make_account(session, tenant=tenant),
        display_name="Other",
    )
    await agent_owners.record_creator(
        session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_w"),
        account_id=owner.account_id,
    )
    await session.commit()
    agents = _Agents(
        ma_agent(
            id="ag_w",
            name="helper",
            tenant_id=tenant.id,
            metadata={"daimon_account": str(owner.account_id)},
            system="Be brief.",
            description="Answers questions",
            version=3,
            updated_at=datetime(2030, 1, 1, tzinfo=UTC),
            **agent_fields,
        )
    )
    return agents, owner, other


async def _token(factory: async_sessionmaker[AsyncSession], person: WebPersonRow) -> str:
    token = new_session_token()
    async with factory() as db, db.begin():
        await identities_store.link_identity(
            db,
            tenant_id=person.tenant_id,
            person_id=person.id,
            provider="discord",
            issuer="",
            subject=str(person.id),
            label="person@example.com",
            groups=(),
        )
        await sessions_store.create_session(
            db,
            token_sha256=token_sha256(token),
            provider="discord",
            issuer="",
            subject=str(person.id),
            people=[person],
            ttl=NOW - datetime(2029, 1, 1, tzinfo=UTC),
            now=NOW,
        )
    return token


@asynccontextmanager
async def _client(
    agents: _Agents,
    tenant_id: uuid.UUID,
    factory: async_sessionmaker[AsyncSession],
    token: str,
    **extra: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    db = {"url": "postgresql+asyncpg://u:p@h/db"}
    settings = extra.pop("settings", None) or Settings(_env_file=None, database=db)  # pyright: ignore[reportCallIssue, reportArgumentType]
    async with httpx.AsyncClient() as http:
        runtime = WebRuntime(
            settings=settings,
            web=extra.pop("web", None) or web_settings(),
            sessionmaker=factory,
            http_client=http,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            anthropic=build_fake_anthropic(agents.router.dispatch),
            now=lambda: NOW,
            **extra,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime)), base_url=PUBLIC_URL
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            yield client


async def test_a_member_reads_the_agent_without_its_prompt(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await _people(db_session)
    token = await _token(db_session_factory, other)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        roster = await client.get("/api/agents")
        agent = await client.get("/api/agents/ag_w")
        refused = await client.patch(
            "/api/agents/ag_w/draft",
            json={"base_version": 3, "config": agent.json()},
            headers=_OWN,
        )
        read_draft = await client.get("/api/agents/ag_w/draft")
        drop_draft = await client.delete("/api/agents/ag_w/draft", headers=_OWN)
    assert roster.status_code == 200, roster.text
    assert (read_draft.status_code, drop_draft.status_code) == (403, 403), (
        "a member who cannot edit the agent cannot read or discard a draft of it either"
    )
    assert [row["id"] for row in roster.json()] == ["ag_w"], "the roster lists the agent"
    assert roster.json()[0]["owner"]["name"] == "Test Person", "the owner is named"
    body = agent.json()
    assert body["system"] is None and body["can_edit"] is False, "a member only reads"
    assert body["nodes"] == [], "no connection node is configured"
    assert "data_access" not in body, "the agent carries no app vocabulary"
    assert refused.status_code == 403, "a member who does not own it cannot draft"


async def test_the_owner_drafts_applies_and_sees_history(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await _people(db_session)
    token = await _token(db_session_factory, owner)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        config = (await client.get("/api/agents/ag_w")).json()
        assert config["system"] == "Be brief.", "the owner reads the prompt"
        config["system"] = "Be thorough."
        draft = await client.patch(
            "/api/agents/ag_w/draft", json={"base_version": 3, "config": config}, headers=_OWN
        )
        assert draft.status_code == 200, draft.text
        assert [c["key"] for c in draft.json()["changes"]] == ["system"], "one change"
        roster = (await client.get("/api/agents")).json()
        assert roster[0]["draft_changes"] == 1, "the roster counts the draft"

        applied = await client.post(
            "/api/agents/ag_w/apply", json={"note": "Wordier"}, headers=_OWN
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["version"] == 4, "apply answers with the new version"
        assert (await client.get("/api/agents/ag_w/draft")).json() is None, "the draft is gone"
        history = (await client.get("/api/agents/ag_w/versions")).json()
        again = await client.post("/api/agents/ag_w/apply", json={}, headers=_OWN)
    assert [(v["version"], v["note"]) for v in history] == [(4, "Wordier"), (3, None)], (
        "history lists the applied version over its base"
    )
    assert history[0]["by"]["name"] == "Test Person", "the author is named"
    assert again.status_code == 409 and again.json()["detail"] == "Nothing to apply"


async def test_apply_after_the_agent_moved_is_409(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await _people(db_session)
    token = await _token(db_session_factory, owner)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        config = (await client.get("/api/agents/ag_w")).json()
        config["purpose"] = "Writes reports"
        await client.patch(
            "/api/agents/ag_w/draft", json={"base_version": 3, "config": config}, headers=_OWN
        )
        agents.agent = agents.agent.model_copy(update={"version": 5})
        stale = await client.post("/api/agents/ag_w/apply", json={}, headers=_OWN)
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == "Changed to v5 since your draft began", "names the version"
    assert agents.updates == [], "nothing is written"


async def _admin(factory: async_sessionmaker[AsyncSession], person: WebPersonRow) -> None:
    async with factory.begin() as db:
        await roles_store.set_workspace_role(
            db,
            tenant_id=person.tenant_id,
            account_id=person.account_id,
            role="owner",
            granted_by_account_id=None,
        )


_CHANNEL = {"id": "c1", "kind": "channel", "platform": "discord", "name": "general"}
_WEB = {"id": "web", "kind": "web", "platform": "web", "name": "Web chat"}


async def test_an_admin_applies_places_without_a_new_agent_version(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await _people(db_session)
    await _admin(db_session_factory, owner)
    token = await _token(db_session_factory, owner)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        config = (await client.get("/api/agents/ag_w")).json()
        assert [p["id"] for p in config["places"]] == ["web"], "an unruled agent answers on the web"
        config["places"] = [{**_CHANNEL, "default": True}, _WEB]
        draft = await client.patch(
            "/api/agents/ag_w/draft", json={"base_version": 3, "config": config}, headers=_OWN
        )
        assert sorted(c["key"] for c in draft.json()["changes"]) == ["place:c1"], draft.text
        applied = await client.post("/api/agents/ag_w/apply", json={}, headers=_OWN)
        assert applied.status_code == 200, applied.text
        after = (await client.get("/api/agents/ag_w")).json()
        roster = (await client.get("/api/agents")).json()
    async with db_session_factory() as db:
        policy = await load_access_policy(db, tenant_id=owner.tenant_id)
        scope = await get_scope(
            db, scope=ChannelScopeRef(tenant_id=owner.tenant_id, channel_id="c1")
        )
    assert agents.updates == [], "places are not an MA field, so the agent is not rewritten"
    assert "helper" not in policy.agent_rules, "an add leaves the agent unrestricted"
    assert scope is not None and scope.agent_name == "helper", "c1 answers with the agent"
    assert [(p["id"], p.get("default")) for p in after["places"]] == [("c1", True), ("web", None)]
    assert roster[0]["runs_in"] == ["discord", "web"], "the roster shows where it runs"


async def test_an_owner_who_is_not_an_admin_cannot_apply_places(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await _people(db_session)
    token = await _token(db_session_factory, owner)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        config = (await client.get("/api/agents/ag_w")).json()
        config["places"] = [{**_CHANNEL, "default": True}, _WEB]
        await client.patch(
            "/api/agents/ag_w/draft", json={"base_version": 3, "config": config}, headers=_OWN
        )
        refused = await client.post("/api/agents/ag_w/apply", json={}, headers=_OWN)
    assert refused.status_code == 403, refused.text
    async with db_session_factory() as db:
        policy = await load_access_policy(db, tenant_id=owner.tenant_id)
    assert "helper" not in policy.agent_rules, "nothing was written"


async def test_the_catalog_offers_this_workspaces_skills_and_configured_channels(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await _people(db_session)
    tenant = str(owner.tenant_id)[:8]
    agents.skills = [
        {
            "id": f"skill_{n}",
            "type": "skill",
            "display_title": title,
            "latest_version": "7",
            "source": source,
            "created_at": "2030-01-01T00:00:00Z",
            "updated_at": "2030-01-01T00:00:00Z",
        }
        for n, (title, source) in enumerate(
            [
                (f"{tenant}-channel-tidy", "custom"),
                ("deadbeef-theirs", "custom"),
                ("xlsx", "anthropic"),
            ]
        )
    ]
    async with db_session_factory.begin() as db:
        await set_fields(
            db,
            scope=ChannelScopeRef(tenant_id=owner.tenant_id, channel_id="c1"),
            tenant_id=owner.tenant_id,
            agent_name="helper",
            mode="agent",
            actor_account_id=owner.account_id,
            set_by_admin=True,
        )
        await platform_names.upsert_channel_names(
            db, tenant_id=owner.tenant_id, platform="discord", names={"c1": "general"}
        )
    token = await _token(db_session_factory, owner)
    async with _client(
        agents,
        owner.tenant_id,
        db_session_factory,
        token,
        skill_descriptions={"channel-tidy": "Keep channels clean."},
    ) as client:
        catalog = (await client.get("/api/agents/catalog")).json()
    assert [(s["type"], s["title"], s["description"], s["version"]) for s in catalog["skills"]] == [
        ("anthropic", "xlsx", "", "7"),
        ("custom", "channel-tidy", "Keep channels clean.", "7"),
    ], "another workspace's skill is left out; a shipped skill carries its description"
    assert [(c["id"], c["name"]) for c in catalog["channels"]] == [("c1", "general")]
    assert "bash" in [t["name"] for t in catalog["tools"]], "built-in tools are listed"
    models = {m["id"]: m for m in catalog["models"]}
    assert {m["backend"] for m in models.values()} == {"anthropic"}, (
        "without provider choice only Anthropic's models are offered"
    )
    assert models["claude-opus-5-5"]["fast"] is True, "a model with a fast variant says so"
    assert not any(i.endswith("-fast") for i in models), "the fast variant is not a model"


async def test_an_owner_switches_the_agent_to_another_providers_model(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The catalog offers every allowed provider, and applying one writes the provider setting."""
    agents, owner, _other = await _people(db_session)
    await _admin(db_session_factory, owner)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        turn=TurnSettings(path="mux", channel_backends=True),
    )
    service = make_backend_service(db_session_factory, settings.turn)
    policy = await service.tenant_policy(owner.tenant_id)
    await service.set_tenant_allowlist(
        owner.tenant_id,
        ("anthropic", "openai"),
        policy.revision,
        BackendActor(
            tenant_id=owner.tenant_id,
            account_id=owner.account_id,
            subject=build_subject(is_admin=True, platform_user_id=None),
        ),
    )
    token = await _token(db_session_factory, owner)
    async with _client(
        agents, owner.tenant_id, db_session_factory, token, settings=settings
    ) as client:
        catalog = (await client.get("/api/agents/catalog")).json()
        config = (await client.get("/api/agents/ag_w")).json()
        config["model"] = {**config["model"], "id": "gpt-6-astra", "backend": "openai"}
        draft = await client.patch(
            "/api/agents/ag_w/draft", json={"base_version": 3, "config": config}, headers=_OWN
        )
        applied = await client.post("/api/agents/ag_w/apply", json={}, headers=_OWN)
        after = (await client.get("/api/agents/ag_w")).json()
    offered = {(m["backend"], m["id"]): m for m in catalog["models"]}
    assert ("openai", "gpt-6-astra") in offered, "an allowed provider's models are offered"
    assert ("gemini", "gemini-4-argon") not in offered, "a provider not allowed here is not"
    assert offered[("openai", "gpt-6-astra")]["in_usd"] > 0, "the model carries its price"
    assert draft.status_code == 200, draft.text
    assert applied.status_code == 200, applied.text
    assert (after["model"]["backend"], after["model"]["id"]) == ("openai", "gpt-6-astra"), (
        "the agent now runs on the chosen provider and model"
    )
    assert all("model" not in update for update in agents.updates), (
        "another provider's model is never written as the Managed Agents model"
    )


async def test_templates_are_blank_plus_the_marked_default_agents(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await _people(db_session)
    token = await _token(db_session_factory, owner)
    writer = AgentSpec(
        name="writer",
        model="claude-sonnet-4-6",
        description="Drafts posts",
        system="Write.",
        template=True,
    )
    async with _client(
        agents, owner.tenant_id, db_session_factory, token, agent_templates=(writer,)
    ) as client:
        listed = (await client.get("/api/agents/templates")).json()
        missing = await client.post(
            "/api/agents", json={"name": "x", "template": "nope"}, headers=_OWN
        )
    assert [(t["id"], t["purpose"]) for t in listed] == [("blank", ""), ("writer", "Drafts posts")]
    assert missing.status_code == 404, "an unknown template is refused"


async def test_the_owner_stores_and_removes_a_servers_token(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    server = {"type": "url", "name": "notes", "url": "https://notes.example.com/mcp"}
    agents, owner, _other = await _people(db_session, mcp_servers=[server])
    token = await _token(db_session_factory, owner)
    owner_id = derive_agent_uuid(tenant_id=owner.tenant_id, ma_agent_id="ag_w")

    async def stored() -> list[str]:
        async with db_session_factory() as db:
            rows = await credentials_store.list_credentials(
                db, tenant_id=owner.tenant_id, agent_id=owner_id
            )
        return [row.mcp_server_url for row in rows]

    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        saved = await client.put(
            "/api/agents/ag_w/mcp/notes/token", json={"token": " secret "}, headers=_OWN
        )
        after_save = await stored()
        unknown = await client.put(
            "/api/agents/ag_w/mcp/nope/token", json={"token": "x"}, headers=_OWN
        )
        removed = await client.delete("/api/agents/ag_w/mcp/notes/token", headers=_OWN)
        after_remove = await stored()

    assert saved.json() == {"ok": True} and after_save == ["https://notes.example.com/mcp"]
    assert unknown.status_code == 404, "only a server the agent has can take a token"
    assert removed.json() == {"ok": True} and after_remove == [], "removing it signs the agent out"


async def test_a_member_who_cannot_edit_cannot_store_a_token(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    server = {"type": "url", "name": "notes", "url": "https://notes.example.com/mcp"}
    agents, owner, other = await _people(db_session, mcp_servers=[server])
    token = await _token(db_session_factory, other)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        refused = await client.put(
            "/api/agents/ag_w/mcp/notes/token", json={"token": "x"}, headers=_OWN
        )
    assert refused.status_code in (403, 404), refused.text


_SKILL_MD = "---\nname: notes\ndescription: Take notes.\n---\nWrite them down.\n"


async def test_a_member_who_cannot_edit_the_agent_cannot_read_or_save_its_skill_files(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, other = await _people(db_session)
    token = await _token(db_session_factory, other)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        read = await client.get("/api/agents/ag_w/skills/sk_1/files?version=1")
        save = await client.post(
            "/api/agents/ag_w/skills",
            json={"skill_id": None, "files": {"SKILL.md": _SKILL_MD}},
            headers=_OWN,
        )
    assert (read.status_code, save.status_code) == (403, 403), "skill files follow edit rights"


async def test_saving_a_skill_with_a_path_outside_the_skill_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await _people(db_session)
    token = await _token(db_session_factory, owner)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        refused = [
            await client.post(
                "/api/agents/ag_w/skills",
                json={"skill_id": None, "files": {"SKILL.md": _SKILL_MD, path: "x"}},
                headers=_OWN,
            )
            for path in ("../evil.md", "/etc/passwd", "refs/../../evil.md")
        ]
    assert [r.status_code for r in refused] == [422, 422, 422], "a path that escapes is refused"
    assert agents.updates == [], "nothing is written to the agent"


async def test_saving_a_skill_file_over_the_size_limit_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    agents, owner, _other = await _people(db_session)
    token = await _token(db_session_factory, owner)
    big = "x" * (agent_skill_files.MAX_EDIT_BYTES + 1)
    async with _client(agents, owner.tenant_id, db_session_factory, token) as client:
        refused = await client.post(
            "/api/agents/ag_w/skills",
            json={"skill_id": None, "files": {"SKILL.md": _SKILL_MD, "big.md": big}},
            headers=_OWN,
        )
    assert refused.status_code == 422, "an oversize body is refused"
    assert agents.updates == [], "nothing is written to the agent"

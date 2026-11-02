"""Agent drafts and versions: the store, then save, apply and revert against a fake MA."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core import agent_changes
from daimon.core.agent_changes import AgentChangeDeps
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_view import AgentViewDeps, PlaceDoc
from daimon.core.authz import Subject, Surface
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_drafts, agent_owners
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, json_body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AT = datetime(2026, 10, 1, tzinfo=UTC)


async def test_store_keeps_one_draft_per_person_and_versions_newest_first(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    t, a = tenant.id, account.id
    for version in (1, 2):
        await agent_drafts.save_draft(
            db_session,
            tenant_id=t,
            agent_id="ag_s",
            account_id=a,
            base_version=version,
            base_config={"b": version},
            config={"v": version},
        )
    draft = await agent_drafts.get_draft(db_session, tenant_id=t, agent_id="ag_s", account_id=a)
    assert draft is not None and (draft.base_version, draft.config) == (2, {"v": 2}), (
        "saving again replaces the person's draft"
    )
    await agent_drafts.delete_draft(db_session, tenant_id=t, agent_id="ag_s", account_id=a)
    gone = await agent_drafts.get_draft(db_session, tenant_id=t, agent_id="ag_s", account_id=a)
    assert gone is None, "a discarded draft is gone"

    for config in ({"v": 1}, {"v": 9}):
        await agent_drafts.record_base(
            db_session, tenant_id=t, agent_id="ag_s", version=1, config=config, at=_AT
        )
    await agent_drafts.record_version(
        db_session,
        tenant_id=t,
        agent_id="ag_s",
        version=2,
        account_id=a,
        note="Tighter prompt",
        changes=[{"key": "system"}],
        config={"v": 2},
    )
    versions = await agent_drafts.list_versions(db_session, tenant_id=t, agent_id="ag_s")
    assert [(v.version, v.config) for v in versions] == [(2, {"v": 2}), (1, {"v": 1})], (
        "newest first, and a base already kept is not overwritten"
    )


class _World:
    """One owned agent whose MA version moves with each update."""

    def __init__(self, agent: BetaManagedAgentsAgent) -> None:
        self.agent = agent
        self.updates: list[dict[str, Any]] = []
        self.router = MARouter()
        self.router.add("GET", r"/v1/agents/ag_d$", lambda _r, _m: self._json())
        self.router.add("POST", r"/v1/agents/ag_d$", self._update)

    def _json(self) -> httpx.Response:
        return httpx.Response(200, json=self.agent.model_dump(mode="json"))

    def _update(self, req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        self.updates.append(body)
        fields = {k: body[k] for k in ("description", "system") if k in body}
        self.agent = self.agent.model_copy(update={"version": self.agent.version + 1, **fields})
        return self._json()

    def bump(self) -> None:
        self.agent = self.agent.model_copy(update={"version": self.agent.version + 1})


async def _setup(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> tuple[_World, AgentChangeDeps, AgentActor]:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    await agent_owners.record_creator(
        session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_d"),
        account_id=account.id,
    )
    await session.commit()
    world = _World(
        ma_agent(
            id="ag_d",
            name="helper",
            tenant_id=tenant.id,
            metadata={"daimon_account": str(account.id)},
            system="Be brief.",
            description="Answers questions",
            version=3,
            updated_at=_AT,
        )
    )
    client = build_fake_anthropic(world.router.dispatch)
    deps = AgentChangeDeps(
        edit=AgentEditDeps(client=client, session_factory=factory, public_url=None),
        view=AgentViewDeps(client=client, session_factory=factory, markup=Decimal(1)),
    )
    actor = AgentActor(
        tenant_id=tenant.id,
        account_id=account.id,
        subject=Subject(is_admin=False, platform_user_id=str(account.id)),
        surface=Surface.WEB,
    )
    return world, deps, actor


async def test_apply_writes_the_draft_and_records_the_version(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world, deps, actor = await _setup(db_session, db_session_factory)
    base = await agent_changes.current_config(deps, tenant_id=actor.tenant_id, agent=world.agent)
    edited = base.model_copy(update={"system": "Be thorough."})
    draft = await agent_changes.save_draft(
        deps, actor=actor, ma_agent_id="ag_d", base_version=3, config=edited
    )
    assert draft is not None and [c.key for c in draft.changes] == ["system"], (
        "the draft lists the prompt change"
    )
    assert draft.changes[0].effect == "session", "a prompt change needs a new session"

    written = await agent_changes.apply_draft(deps, actor=actor, ma_agent_id="ag_d", note=" v4 ")
    assert written.version == 4, "apply returns the version MA wrote"
    assert world.updates[0]["system"].endswith("Be thorough."), "the prompt is written"
    async with db_session_factory() as session:
        (latest, first) = await agent_drafts.list_versions(
            session, tenant_id=actor.tenant_id, agent_id="ag_d"
        )
        left = await agent_drafts.get_draft(
            session, tenant_id=actor.tenant_id, agent_id="ag_d", account_id=actor.account_id
        )
    assert (latest.version, latest.note, latest.account_id) == (4, "v4", actor.account_id), (
        "the applied version is recorded with its author and note"
    )
    assert first.version == 3 and first.account_id is None, "the base is kept for history"
    assert left is None, "the applied draft is gone"

    reverted = await agent_changes.revert(deps, actor=actor, ma_agent_id="ag_d", version=3)
    assert reverted.version == 5 and (world.agent.system or "").endswith("\n\nBe brief."), (
        "a revert writes the old prompt as a new version"
    )


async def test_apply_refuses_a_draft_begun_on_an_older_version(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world, deps, actor = await _setup(db_session, db_session_factory)
    base = await agent_changes.current_config(deps, tenant_id=actor.tenant_id, agent=world.agent)
    await agent_changes.save_draft(
        deps,
        actor=actor,
        ma_agent_id="ag_d",
        base_version=3,
        config=base.model_copy(update={"purpose": "Writes reports"}),
    )
    world.bump()
    world.bump()

    draft = await agent_changes.load_draft(deps, actor=actor, agent=world.agent)
    assert draft is not None and draft.stale, "the draft shows it went stale"
    with pytest.raises(AgentEditError) as raised:
        await agent_changes.apply_draft(deps, actor=actor, ma_agent_id="ag_d", note=None)
    assert raised.value.kind == "stale", "a stale draft is refused (409)"
    assert str(raised.value) == "Changed to v5 since your draft began", "it names the version"
    assert world.updates == [], "nothing is written"


async def test_save_refuses_an_unknown_older_base(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    world, deps, actor = await _setup(db_session, db_session_factory)
    base = await agent_changes.current_config(deps, tenant_id=actor.tenant_id, agent=world.agent)
    with pytest.raises(AgentEditError) as raised:
        await agent_changes.save_draft(
            deps, actor=actor, ma_agent_id="ag_d", base_version=1, config=base
        )
    assert raised.value.kind == "stale", "a draft cannot begin from a version never recorded"


async def test_apply_refuses_a_draft_when_places_moved_under_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Places and tool rules change without a new provider version; the draft still goes stale."""
    world, deps, actor = await _setup(db_session, db_session_factory)
    base = await agent_changes.current_config(deps, tenant_id=actor.tenant_id, agent=world.agent)
    await agent_changes.save_draft(
        deps,
        actor=actor,
        ma_agent_id="ag_d",
        base_version=3,
        config=base.model_copy(update={"purpose": "Writes reports"}),
    )
    async with db_session_factory.begin() as session:
        moved = base.model_copy(
            update={"places": [PlaceDoc(id="c9", kind="channel", platform="discord", name="x")]}
        )
        await agent_drafts.save_draft(
            session,
            tenant_id=actor.tenant_id,
            agent_id="ag_d",
            account_id=actor.account_id,
            base_version=3,
            base_config=moved.model_dump(mode="json"),
            config=base.model_copy(update={"purpose": "Writes reports"}).model_dump(mode="json"),
        )
    with pytest.raises(AgentEditError) as raised:
        await agent_changes.apply_draft(deps, actor=actor, ma_agent_id="ag_d", note=None)
    assert raised.value.kind == "stale", "a draft begun on other places is refused (409)"
    assert world.updates == [], "nothing is written"


async def test_two_applies_on_one_version_keep_both_history_notes(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A place-only apply does not bump the version; it must not erase the row's history."""
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        for note, key in (("first", "a"), ("second", "b")):
            await agent_drafts.record_version(
                session,
                tenant_id=tenant.id,
                agent_id="ag_h",
                version=2,
                config={"k": key},
                changes=[{"key": key}],
                account_id=account.id,
                note=note,
            )
        (row,) = await agent_drafts.list_versions(session, tenant_id=tenant.id, agent_id="ag_h")
    assert row.note == "first / second", "both notes are kept"
    assert [c["key"] for c in row.changes] == ["a", "b"], "both change lists are kept"
    assert row.config == {"k": "b"}, "the config is the newest"

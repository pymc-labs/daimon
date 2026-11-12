"""Durability, routing, ownership, CAS and immutable bundle retention on Postgres."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal, NoReturn, cast

import pytest
import pytest_asyncio
from daimon.core._models import (
    NeutralAgentRevision,
    NeutralSkillVersion,
    UserSkill,
)
from daimon.core.purge import purge_account
from daimon.core.skills.ingest import SkillBundle, bundle_from_files, bundle_from_markdown
from daimon.core.stores.neutral_catalog import CatalogConflict, PostgresNeutralCatalog
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import (
    ChannelRef,
    ModelRef,
    ResourceRef,
    Revision,
    Scope,
    SkillRef,
    ThreadRef,
)
from mux.contracts.resources import Agent, AgentSpec
from mux.errors import ScopeViolation
from mux.state.lease import Slot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 10, tzinfo=UTC)
MARKDOWN = "---\nname: planning\ndescription: Plan tasks\n---\nMake a plan.\n"
type Provider = Literal["openai", "gemini"]
type Setup = tuple[async_sessionmaker[AsyncSession], Scope, Scope, Scope]


@pytest_asyncio.fixture
async def setup(
    db_engine: AsyncEngine,
    db_clean: None,
) -> AsyncIterator[Setup]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
        other_tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        other_account = await make_account(session, tenant=tenant)
        foreign_account = await make_account(session, tenant=other_tenant)
        scopes = tuple(
            Scope(
                tenant_id=str(t),
                account_id=str(a),
                principal_id="principal",
                authorization_id="host-policy",
            )
            for t, a in (
                (tenant.id, account.id),
                (tenant.id, other_account.id),
                (other_tenant.id, foreign_account.id),
            )
        )
    yield sessions, scopes[0], scopes[1], scopes[2]


def agent(scope: Scope, provider: Provider, native_id: str = "native-1") -> Agent:
    return Agent(
        ref=ResourceRef(
            id=native_id,
            kind="agent",
            provider=provider,
            account_scope_id="workspace",
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
        ),
        revision=Revision(local=0),
        created_at=NOW,
        spec=AgentSpec(
            name="dev",
            model=ModelRef(provider=provider, id="cheap"),
            skills=(SkillRef(id="s", version="v1"), SkillRef(id="s", version="v1")),
        ),
        native={"id": native_id, "daimon_channel": None, "daimon_thread": None},
    )


def store(
    sessions: async_sessionmaker[AsyncSession],
    provider: Provider = "openai",
    workspace: str = "workspace",
) -> PostgresNeutralCatalog:
    return PostgresNeutralCatalog(sessions, provider=provider, account_scope_id=workspace)


@pytest.mark.parametrize("provider", ("openai", "gemini"))
async def test_agent_edits_are_durable_native_replacements_with_pinned_history(
    setup: Setup, provider: Provider
) -> None:
    sessions, scope, _, _ = setup
    catalog = store(sessions, provider)
    first = await catalog.put_agent(scope, "logical", agent(scope, provider), expected_revision=0)
    second = await catalog.put_agent(
        scope, "logical", agent(scope, provider, "native-2"), expected_revision=1
    )
    restarted = store(sessions, provider)
    assert await restarted.get_agent(scope, "logical") == second
    assert await restarted.get_agent(scope, "logical", local_revision=1) == first
    assert await restarted.list_agents(scope) == (second,)
    assert second.local_revision == 2
    assert second.agent.revision == Revision(local=0), "never invent native revisions"
    assert first.agent.ref.id == "native-1" and second.agent.ref.id == "native-2"
    assert first.agent.spec.skills == second.agent.spec.skills, "order and duplicate pins survive"
    assert first.agent.native == {"id": "native-1", "daimon_channel": None, "daimon_thread": None}
    with pytest.raises(CatalogConflict):
        await restarted.put_agent(
            scope, "logical", agent(scope, provider, "stale"), expected_revision=1
        )
    assert await restarted.get_agent(scope, "logical") == second


@pytest.mark.parametrize("existing", (False, True))
async def test_competing_agent_writers_have_one_winner(setup: Setup, existing: bool) -> None:
    sessions, scope, _, _ = setup
    catalog = store(sessions)
    revision = 0
    if existing:
        await catalog.put_agent(scope, "logical", agent(scope, "openai"), expected_revision=0)
        revision = 1
    results = await asyncio.gather(
        *(
            store(sessions).put_agent(
                scope, "logical", agent(scope, "openai", native), expected_revision=revision
            )
            for native in ("left", "right")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CatalogConflict) for result in results) == 1
    latest = await catalog.get_agent(scope, "logical")
    assert latest is not None and latest.local_revision == revision + 1
    assert latest.agent.ref.id in ("left", "right")


@pytest.mark.parametrize("provider", ("openai", "gemini"))
async def test_owned_bundle_is_durable_and_byte_exact(setup: Setup, provider: Provider) -> None:
    sessions, scope, _, _ = setup
    catalog = store(sessions, provider)
    bundle = bundle_from_files({"SKILL.md": MARKDOWN.encode(), "assets/raw.bin": bytes(range(256))})
    saved = await catalog.put_skill(
        scope,
        skill_id="skill",
        version="opaque-1",
        agent_name="dev",
        display_title="dev planning",
        bundle=bundle,
    )
    restarted = store(sessions, provider)
    download = await restarted.get_skill(scope, "skill", "opaque-1")
    assert download is not None
    assert download == saved and download.bundle.zip_bytes == bundle.zip_bytes
    assert download.display_title == "dev planning" and download.agent_name == "dev"
    assert download.principal_id == scope.principal_id
    assert await restarted.list_skills(scope) == (saved,)
    assert (
        await restarted.put_skill(
            scope,
            skill_id="skill",
            version="opaque-1",
            agent_name="dev",
            display_title="dev planning",
            bundle=bundle,
        )
        == saved
    )
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(UserSkill)) == 0


async def test_skill_versions_are_immutable_and_owner_cannot_change(setup: Setup) -> None:
    sessions, scope, _, _ = setup
    catalog = store(sessions)
    bundle = bundle_from_markdown(MARKDOWN)
    await catalog.put_skill(
        scope,
        skill_id="skill",
        version="v1",
        agent_name="dev",
        display_title="dev planning",
        bundle=bundle,
    )
    with pytest.raises(CatalogConflict):
        await catalog.put_skill(
            scope,
            skill_id="skill",
            version="v1",
            agent_name="dev",
            display_title="changed",
            bundle=bundle,
        )
    for owner, caller in (
        ("foreign", scope),
        ("dev", scope.model_copy(update={"principal_id": "foreign"})),
    ):
        with pytest.raises(ScopeViolation):
            await catalog.put_skill(
                caller,
                skill_id="skill",
                version="v2",
                agent_name=owner,
                display_title="dev planning",
                bundle=bundle,
            )
    assert len(await catalog.list_skills(scope)) == 1


async def test_catalog_isolated_by_tenant_account_provider_and_workspace(setup: Setup) -> None:
    sessions, scope, account, tenant = setup
    catalog = store(sessions)
    await catalog.put_agent(scope, "logical", agent(scope, "openai"), expected_revision=0)
    await catalog.put_skill(
        scope,
        skill_id="skill",
        version="v1",
        agent_name="dev",
        display_title="dev planning",
        bundle=bundle_from_markdown(MARKDOWN),
    )
    for other, caller in (
        (catalog, account),
        (catalog, tenant),
        (store(sessions, "gemini"), scope),
        (store(sessions, workspace="foreign"), scope),
    ):
        assert await other.get_agent(caller, "logical") is None
        assert await other.get_skill(caller, "skill", "v1") is None
        assert await other.list_agents(caller) == ()
        assert await other.list_skills(caller) == ()
    mismatched = scope.model_copy(update={"account_id": tenant.account_id})
    with pytest.raises(ScopeViolation):
        await catalog.get_agent(mismatched, "logical")
    with pytest.raises(ScopeViolation):
        await catalog.put_agent(scope, "logical", agent(account, "openai"), expected_revision=1)


@pytest.mark.parametrize(
    "scope",
    (
        Scope.platform(reason="operator"),
        Scope.legacy_host_authorized(call_site="old"),
        Scope(tenant_id="invalid", account_id="invalid", principal_id="p", authorization_id="a"),
    ),
)
async def test_invalid_scope_refuses_before_opening_session(scope: Scope) -> None:
    class NoIO:
        def __call__(self) -> NoReturn:
            raise AssertionError("authorization must precede database I/O")

    catalog = PostgresNeutralCatalog(
        cast(async_sessionmaker[AsyncSession], NoIO()),
        provider="openai",
        account_scope_id="workspace",
    )
    with pytest.raises(ScopeViolation):
        await catalog.get_agent(scope, "logical")


async def test_unchecked_preview_and_latest_pin_refused(setup: Setup) -> None:
    sessions, scope, _, _ = setup
    catalog = store(sessions)
    bundle = bundle_from_markdown(MARKDOWN)
    forged = SkillBundle(
        bundle.preview.model_copy(update={"content_hash": "forged"}), bundle.zip_bytes
    )
    for candidate, version in ((forged, "v1"), (bundle, "latest")):
        with pytest.raises(ValueError):
            await catalog.put_skill(
                scope,
                skill_id="skill",
                version=version,
                agent_name="dev",
                display_title="dev planning",
                bundle=candidate,
            )
    assert await catalog.list_skills(scope) == ()


async def test_concurrent_skill_claims_cannot_transfer_ownership(setup: Setup) -> None:
    sessions, scope, _, _ = setup
    bundle = bundle_from_markdown(MARKDOWN)
    results = await asyncio.gather(
        *(
            store(sessions).put_skill(
                scope,
                skill_id="skill",
                version=version,
                agent_name=owner,
                display_title=f"{owner} planning",
                bundle=bundle,
            )
            for version, owner in (("v1", "left"), ("v2", "right"))
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ScopeViolation) for result in results) == 1
    assert len(await store(sessions).list_skills(scope)) == 1


@pytest.mark.parametrize("provider", ("openai", "gemini"))
async def test_account_purge_cascades_catalog_data_and_preserves_other_account(
    setup: Setup,
    provider: Provider,
) -> None:
    sessions, scope, other, _ = setup
    catalog = store(sessions, provider)
    bundle = bundle_from_markdown(MARKDOWN)
    for caller in (scope, other):
        await catalog.put_agent(caller, "logical", agent(caller, provider), expected_revision=0)
        await catalog.put_skill(
            caller,
            skill_id="skill",
            version="v1",
            agent_name="dev",
            display_title="dev planning",
            bundle=bundle,
        )
    result = await purge_account(sm=sessions, account_id=uuid.UUID(scope.account_id))
    assert result.db.accounts == 1
    async with sessions() as session:
        for table in (NeutralAgentRevision, NeutralSkillVersion):
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.account_id == uuid.UUID(scope.account_id))
                )
                == 0
            )
    assert len(await catalog.list_agents(other)) == 1
    assert len(await catalog.list_skills(other)) == 1
    with pytest.raises(ScopeViolation):
        await catalog.list_agents(scope)


async def test_a_shared_threads_copy_belongs_to_its_owner_and_no_person(setup: Setup) -> None:
    from daimon.core.shared_threads import shared_owner

    sessions, scope, other, foreign = setup
    channel = ChannelRef(tenant_id=scope.tenant_id, platform="slack", channel_id="C1")
    slot = Slot(thread=ThreadRef(channel=channel, thread_id="t1"))
    owner = scope.model_copy(update={"account_id": str(shared_owner(slot))})
    shared = PostgresNeutralCatalog(
        sessions, provider="openai", account_scope_id="workspace", shared_slot=slot
    )
    await shared.put_agent(owner, "logical", agent(owner, "openai"), expected_revision=0)
    assert len(await shared.list_agents(owner)) == 1
    # A person, another thread's owner or another tenant never reaches it.
    elsewhere = Slot(thread=ThreadRef(channel=channel, thread_id="t2"))
    stranger = scope.model_copy(update={"account_id": str(shared_owner(elsewhere))})
    for caller in (
        scope,
        other,
        stranger,
        foreign.model_copy(update={"account_id": owner.account_id}),
    ):
        with pytest.raises(ScopeViolation):
            await shared.list_agents(caller)
    with pytest.raises(ScopeViolation):
        await store(sessions).list_agents(owner)


async def _shared_copy(
    sessions: async_sessionmaker[AsyncSession],
    scope: Scope,
    thread_id: str,
    catalog_id: str,
    *,
    channel_id: str = "C1",
) -> uuid.UUID:
    """A shared thread's agent copy and one skill copy, held by the thread's owner."""
    from daimon.core.shared_threads import shared_owner

    channel = ChannelRef(tenant_id=scope.tenant_id, platform="slack", channel_id=channel_id)
    slot = Slot(thread=ThreadRef(channel=channel, thread_id=thread_id))
    owner = shared_owner(slot)
    owned = scope.model_copy(update={"account_id": str(owner)})
    catalog = PostgresNeutralCatalog(
        sessions, provider="openai", account_scope_id="workspace", shared_slot=slot
    )
    await catalog.put_agent(owned, catalog_id, agent(owned, "openai"), expected_revision=0)
    await catalog.put_skill(
        owned,
        skill_id=f"skill-{thread_id}",
        version="v1",
        agent_name="dev",
        display_title="dev planning",
        bundle=bundle_from_markdown(MARKDOWN),
    )
    return owner


async def _rows(sessions: async_sessionmaker[AsyncSession], owner: uuid.UUID) -> tuple[int, int]:
    """(agent revisions, skill versions) one owner holds."""
    async with sessions() as session:
        agents = await session.scalar(
            select(func.count())
            .select_from(NeutralAgentRevision)
            .where(NeutralAgentRevision.account_id == owner)
        )
        skills = await session.scalar(
            select(func.count())
            .select_from(NeutralSkillVersion)
            .where(NeutralSkillVersion.account_id == owner)
        )
    return agents or 0, skills or 0


async def test_deleting_an_agent_removes_the_copies_shared_threads_hold(setup: Setup) -> None:
    """Shared owners have no account row, so the agent's deletion must reach them itself."""
    from daimon.core.stores.neutral_catalog import delete_shared_agent_copies

    sessions, scope, _, _ = setup
    gone, kept = uuid.uuid4(), uuid.uuid4()
    held = await _shared_copy(sessions, scope, "t1", f"root:{gone}:1:openai")
    other = await _shared_copy(sessions, scope, "t2", f"root:{kept}:1:openai")
    # A person's own copy of the same agent leaves with the person, not with this call.
    await store(sessions).put_agent(
        scope, f"root:{gone}:1:openai", agent(scope, "openai"), expected_revision=0
    )
    async with sessions() as session, session.begin():
        await delete_shared_agent_copies(
            session, tenant_id=uuid.UUID(scope.tenant_id), agent_id=gone
        )
    assert await _rows(sessions, held) == (0, 0), "the thread keeps no copy of a deleted agent"
    assert await _rows(sessions, other) == (1, 1), "another agent's shared copy stays"
    assert await _rows(sessions, uuid.UUID(scope.account_id)) == (1, 0), "a person's copy stays"


async def test_deleting_a_thread_removes_its_owners_catalog_rows(setup: Setup) -> None:
    """A deleted thread's copy of a person's agent is not kept for good."""
    from daimon.core.stores.thread_agent_bindings import update_lifecycle

    sessions, scope, _, _ = setup
    tenant = uuid.UUID(scope.tenant_id)
    deleted = await _shared_copy(sessions, scope, "t1", "root:a:1:openai")
    other = await _shared_copy(sessions, scope, "t2", "root:a:1:openai")
    async with sessions() as session, session.begin():
        await update_lifecycle(
            session,
            tenant_id=tenant,
            platform="slack",
            parent_channel_id="C1",
            thread_id="t1",
            archived=True,
        )
    assert await _rows(sessions, deleted) == (1, 1), "an archived thread can come back"
    async with sessions() as session, session.begin():
        await update_lifecycle(
            session,
            tenant_id=tenant,
            platform="slack",
            parent_channel_id="C1",
            thread_id="t1",
            deleted=True,
        )
    assert await _rows(sessions, deleted) == (0, 0), "the deleted thread's rows are gone"
    assert await _rows(sessions, other) == (1, 1), "another thread's rows stay"


async def test_deleting_a_channel_removes_its_threads_owners_catalog_rows(setup: Setup) -> None:
    from daimon.core.stores.thread_agent_bindings import create_binding, update_channel_lifecycle

    sessions, scope, _, _ = setup
    tenant = uuid.UUID(scope.tenant_id)
    inside = await _shared_copy(sessions, scope, "t1", "root:a:1:openai")
    elsewhere = await _shared_copy(sessions, scope, "t1", "root:a:1:openai", channel_id="C2")
    async with sessions() as session, session.begin():
        await create_binding(
            session,
            tenant_id=tenant,
            platform="slack",
            parent_channel_id="C1",
            thread_id="t1",
            responder_ma_agent_id="agent_1",
            responder_name="dev",
            kind="handoff",
        )
        await update_channel_lifecycle(
            session, tenant_id=tenant, platform="slack", parent_channel_id="C1", deleted=True
        )
    assert await _rows(sessions, inside) == (0, 0), "the channel's thread loses its copy"
    assert await _rows(sessions, elsewhere) == (1, 1), "a thread of another channel keeps it"

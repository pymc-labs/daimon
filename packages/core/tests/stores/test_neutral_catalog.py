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
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope, SkillRef
from mux.contracts.resources import Agent, AgentSpec
from mux.errors import ScopeViolation
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

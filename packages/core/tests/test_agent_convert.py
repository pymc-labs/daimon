"""Converting a Managed Agents agent to a Daimon record: copy, then delete the MA twin."""

import ast
import inspect
import io
import re
import uuid
import zipfile
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx
import pytest
from daimon.core import agent_convert, agent_lifecycle
from daimon.core._models import NeutralSkillVersion
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.agent_convert import convert_agent_to_anthropic, convert_agent_to_daimon
from daimon.core.authz import Subject
from daimon.core.defaults.ma_index import SkillVersionNotFoundError
from daimon.core.defaults.metadata import MA_METADATA_KEY_CONVERTED_FROM
from daimon.core.errors import UserFacingError
from daimon.core.mux_backend import resource_scope
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.skills.add import SkillAddResult
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.core.turn import session_skills
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.contracts.ids import SkillRef
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DEFAULT_KEY_ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(DEFAULT_KEY_ENV["openai"], "test-key")


async def test_convert_keeps_rules_and_archives_the_twin_raw(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record takes the name and rules; the MA twin is archived, never forgotten."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_rules={"analyst": AgentRule(invoker_role_ids=("111",))}),
    )
    await db_session.commit()
    source = ma_agent(id="agent_ma1", name="analyst", tenant_id=tenant.id, system="Analyse.")
    archived: list[str] = []

    def archive(_req: httpx.Request, match: re.Match[str]) -> httpx.Response:
        archived.append(match.group(1))
        body = source.model_dump(mode="json") | {"archived_at": "2026-11-10T00:00:00Z"}
        return httpx.Response(200, json=body)

    async def forget(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        raise AssertionError("convert must not run lifecycle archive")

    monkeypatch.setattr(agent_lifecycle, "forget_archived_agent", forget)
    router = MARouter()
    router.add_agent_list(source)
    router.add("POST", r"/v1/agents/([^/]+)/archive", archive)
    converted = await convert_agent_to_daimon(
        build_fake_anthropic(router.dispatch),
        db_session_factory,
        tenant_id=tenant.id,
        name="analyst",
        subject=Subject(is_admin=True),
    )

    assert converted.agent.id.startswith("dm_agent_"), "the copy is a Daimon record"
    record = await SelfHostedAgents(db_session_factory).resolve(
        resource_scope(tenant_id=str(tenant.id)), "analyst"
    )
    assert record.spec.model.provider == "openai", "the record runs on the keyed provider"
    assert record.spec.system == "Analyse.", "the definition carries over"
    assert record.spec.metadata.get(MA_METADATA_KEY_CONVERTED_FROM) == "agent_ma1", (
        "the record names its old id, so tokens signed for it keep working"
    )
    assert archived == ["agent_ma1"], f"the MA twin is archived once: {archived}"
    db_session.expire_all()
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert "analyst" in policy.agent_rules, "the name's rule survives the conversion"


async def test_convert_is_admin_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A non-admin is refused before any provider call."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    with pytest.raises(UserFacingError, match="admin"):
        await convert_agent_to_daimon(
            build_fake_anthropic(MARouter().dispatch),
            db_session_factory,
            tenant_id=tenant.id,
            name="analyst",
            subject=Subject(is_admin=False),
        )


def test_convert_never_reaches_lifecycle_archive() -> None:
    """Contract: convert deletes through the raw port, never the name-keyed lifecycle path."""
    tree = ast.parse(inspect.getsource(agent_convert))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    banned = {"forget_archived_agent", "archive_agent", "archive_agent_everywhere"}
    assert not names & banned, f"convert must not call lifecycle archive: {names & banned}"


def _skill_zip(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{name}/SKILL.md", f"---\nname: {name}\ndescription: Do {name}.\n---\nBody"
        )
    return buffer.getvalue()


_SKILLS = (
    {"type": "custom", "skill_id": "skill_a", "version": "1"},
    {"type": "custom", "skill_id": "skill_b", "version": "2"},
    {"type": "anthropic", "skill_id": "xlsx", "version": "latest"},
)


def _skill_router(tenant_id: uuid.UUID, *, fail_archive: int = 0) -> MARouter:
    source = ma_agent(id="agent_ma1", name="analyst", tenant_id=tenant_id, skills=_SKILLS)
    left = [fail_archive]

    def archive(_req: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        if left[0]:
            left[0] -= 1
            return httpx.Response(400, json={"error": {"type": "invalid_request_error"}})
        body = source.model_dump(mode="json") | {"archived_at": "2026-11-10T00:00:00Z"}
        return httpx.Response(200, json=body)

    router = MARouter()
    router.add_agent_list(source)
    router.add("POST", r"/v1/agents/([^/]+)/archive", archive)
    return router


def _downloads(monkeypatch: pytest.MonkeyPatch, *, refuse: str | None = None) -> None:
    async def download(_client: object, *, skill_id: str, version: str, scope: object) -> bytes:
        if skill_id == refuse:
            raise SkillVersionNotFoundError(skill_id)
        return _skill_zip(f"{skill_id.replace('_', '-')}-v{version}")

    monkeypatch.setattr(agent_convert, "download_skill_version", download)


async def _to_daimon(
    sessions: async_sessionmaker[AsyncSession],
    router: MARouter,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
) -> agent_convert.ConvertedAgent:
    return await convert_agent_to_daimon(
        build_fake_anthropic(router.dispatch),
        sessions,
        tenant_id=tenant_id,
        name="analyst",
        subject=Subject(is_admin=True),
        account_id=account_id,
    )


async def _catalog_rows(db: AsyncSession) -> list[tuple[str, str, uuid.UUID]]:
    db.expire_all()
    rows = await db.execute(
        select(
            NeutralSkillVersion.skill_id,
            NeutralSkillVersion.version,
            NeutralSkillVersion.account_id,
        )
    )
    return sorted(tuple(row) for row in rows)


async def test_convert_copies_custom_skills_as_the_person_and_survives_a_rerun(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom skills land in the catalog as the person's, pinned once even after a crash."""
    tenant = await make_tenant(db_session)
    person = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    _downloads(monkeypatch)
    router = _skill_router(tenant.id, fail_archive=1)

    with pytest.raises(anthropic.BadRequestError):
        await _to_daimon(db_session_factory, router, tenant.id, person.id)
    converted = await _to_daimon(db_session_factory, router, tenant.id, person.id)

    assert converted.copied_skills == ("skill-a-v1", "skill-b-v2"), (
        f"both custom skills are copied: {converted.copied_skills}"
    )
    assert converted.dropped_skills == ("xlsx (built in)",), (
        f"the built-in one is named: {converted.dropped_skills}"
    )
    assert await _catalog_rows(db_session) == [
        ("skill_a", "1", person.id),
        ("skill_b", "2", person.id),
    ], "one catalog copy each, owned by the person, after the re-run"
    scope = resource_scope(tenant_id=str(tenant.id))
    record = await SelfHostedAgents(db_session_factory).resolve(scope, "analyst")
    assert record.spec.skills == (
        SkillRef(id="skill_a", version="1", source="custom"),
        SkillRef(id="skill_b", version="2", source="custom"),
    ), f"the record pins each skill once: {record.spec.skills}"
    catalog = PostgresNeutralCatalog(
        db_session_factory, provider="selfhosted", account_scope_id=str(tenant.id)
    )
    pin = next(iter(record.spec.skills or ()))
    person_scope = resource_scope(tenant_id=str(tenant.id), account_id=str(person.id))
    stored = await catalog.get_agent_skill(person_scope, record, pin)
    assert stored is not None, "the record's pin is attached, so its turns load the skill"


async def test_convert_lists_a_skill_it_could_not_download(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One refused download is named; the others still copy and the move finishes."""
    tenant = await make_tenant(db_session)
    person = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    _downloads(monkeypatch, refuse="skill_b")

    converted = await _to_daimon(db_session_factory, _skill_router(tenant.id), tenant.id, person.id)

    assert converted.agent.id.startswith("dm_agent_"), "the move finishes"
    assert converted.copied_skills == ("skill-a-v1",), f"the other copies: {converted}"
    assert converted.dropped_skills == (
        "xlsx (built in)",
        "skill_b (couldn't be downloaded)",
    ), f"the refused one is named in plain words: {converted.dropped_skills}"


async def test_convert_without_a_person_only_lists_the_skills(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run as the operator, no one would own a copy: the skills are listed, not copied."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    _downloads(monkeypatch, refuse="skill_a")

    converted = await _to_daimon(db_session_factory, _skill_router(tenant.id), tenant.id, None)

    assert converted.copied_skills == (), "nothing is copied"
    assert converted.dropped_skills == (
        "skill_a (not copied: no person ran the move)",
        "skill_b (not copied: no person ran the move)",
        "xlsx (built in)",
    ), f"each skill is listed with why: {converted.dropped_skills}"
    assert await _catalog_rows(db_session) == [], "no catalog copy without an owner"
    assert agent_convert.skill_receipt(converted)[-1].startswith("Skills left off: skill_a"), (
        "the receipt names them"
    )


async def test_convert_back_adds_the_records_skills_to_claude_as_the_person(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daimon to Claude: each catalog skill is added to the new agent, owned by the person."""
    tenant = await make_tenant(db_session)
    person = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    _downloads(monkeypatch, refuse="skill_b")
    await _to_daimon(db_session_factory, _skill_router(tenant.id), tenant.id, person.id)
    created = ma_agent(id="agent_ma2", name="analyst", tenant_id=tenant.id)
    added: list[tuple[str, str, uuid.UUID | None]] = []

    async def no_tagged(*_a: object, **_k: object) -> list[object]:
        return []

    async def create(*_a: object, **_k: object) -> object:
        return created

    async def retrieve(*_a: object, **_k: object) -> object:
        return created

    async def add(
        *_a: object, agent: Any, bundle: Any, added_by_account_id: uuid.UUID | None, **_k: object
    ) -> SkillAddResult:
        added.append((agent.id, bundle.preview.name, added_by_account_id))
        return SkillAddResult(
            name=bundle.preview.name,
            skill_id="skill_new",
            version="1",
            action="created",
            newly_attached=True,
        )

    monkeypatch.setattr(agent_convert, "find_agents_by_daimon_tag", no_tagged)
    monkeypatch.setattr(agent_convert, "create_agent", create)
    monkeypatch.setattr(agent_convert, "retrieve_agent", retrieve)
    monkeypatch.setattr(agent_convert, "add_agent_skill", add)
    converted = await convert_agent_to_anthropic(
        build_fake_anthropic(MARouter().dispatch),
        db_session_factory,
        tenant_id=tenant.id,
        name="analyst",
        subject=Subject(is_admin=True),
        public_url=None,
        account_id=person.id,
    )

    assert added == [("agent_ma2", "skill-a-v1", person.id)], f"added as the person: {added}"
    assert converted.copied_skills == ("skill-a-v1",), f"the receipt names it: {converted}"


async def test_a_rerun_by_another_admin_keeps_the_first_copy(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copying onto a record that already holds the skills, as someone else, writes nothing."""
    tenant = await make_tenant(db_session)
    first = await make_account(db_session, tenant=tenant)
    second = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    _downloads(monkeypatch)
    router = _skill_router(tenant.id)
    converted = await _to_daimon(db_session_factory, router, tenant.id, first.id)
    source = ma_agent(id="agent_ma1", name="analyst", tenant_id=tenant.id, skills=_SKILLS)

    copied, kept, dropped = await agent_convert._copy_skills_to_record(  # pyright: ignore[reportPrivateUsage]
        build_fake_anthropic(router.dispatch),
        db_session_factory,
        tenant_id=tenant.id,
        name="analyst",
        source=source,
        record_id=converted.agent.id,
        account_id=second.id,
    )

    assert (copied, kept) == ((), ("skill-a-v1", "skill-b-v2")), "nothing is copied twice"
    assert dropped == ("xlsx (built in)",)
    receipt = agent_convert.skill_receipt(
        agent_convert.ConvertedAgent(converted.agent, dropped, copied, kept)
    )
    assert receipt == (
        "Skills already copied: skill-a-v1, skill-b-v2.",
        "Skills left off: xlsx (built in).",
    ), receipt
    assert await _catalog_rows(db_session) == [
        ("skill_a", "1", first.id),
        ("skill_b", "2", first.id),
    ], "the first copies stay the only ones"


async def test_pinning_a_skill_again_replaces_its_other_version(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pin per skill: a newer version replaces the held one instead of adding a second."""
    tenant = await make_tenant(db_session)
    person = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    _downloads(monkeypatch)
    converted = await _to_daimon(db_session_factory, _skill_router(tenant.id), tenant.id, person.id)
    person_scope = resource_scope(tenant_id=str(tenant.id), account_id=str(person.id))
    newer = SkillRef(id="skill_a", version="3", source="custom")

    await agent_convert._pin_record_skills(  # pyright: ignore[reportPrivateUsage]
        db_session_factory, person_scope, converted.agent.id, (newer,)
    )

    record = await SelfHostedAgents(db_session_factory).resolve(
        resource_scope(tenant_id=str(tenant.id)), "analyst"
    )
    assert record.spec.skills == (SkillRef(id="skill_b", version="2", source="custom"), newer), (
        f"skill_a is pinned once, at the new version: {record.spec.skills}"
    )


async def test_a_converted_record_loads_its_skills_after_the_original_is_gone(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measure and copy for another provider read the record's catalog copy, not the MA one."""
    tenant = await make_tenant(db_session)
    person = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    _downloads(monkeypatch)
    await _to_daimon(db_session_factory, _skill_router(tenant.id), tenant.id, person.id)

    async def deleted(*_a: object, skill_id: str, **_k: object) -> bytes:
        raise SkillVersionNotFoundError(skill_id)

    uploaded: list[tuple[str, ...]] = []

    class Skills:
        async def create(self, _scope: object, upload: Any, **_k: object) -> SimpleNamespace:
            uploaded.append(tuple(item.path for item in upload.files))
            return SimpleNamespace(id="copy-a", latest_version=SimpleNamespace(version="1"))

    monkeypatch.setattr(session_skills, "download_skill_version", deleted)
    record = await SelfHostedAgents(db_session_factory).resolve(
        resource_scope(tenant_id=str(tenant.id)), "analyst"
    )
    person_scope = resource_scope(tenant_id=str(tenant.id), account_id=str(person.id))
    anthropic_client = build_fake_anthropic(MARouter().dispatch)
    sizes = await session_skills.measure_skills(
        db_session_factory,
        anthropic_client,
        scope=person_scope,
        tenant_id=tenant.id,
        pins=(("skill_a", "1"),),
        record=record,
    )
    assert ("skill_a", "1") in sizes, "the size is measured from the catalog copy"
    copies, losses = await session_skills.copy_skills(
        db_session_factory,
        anthropic_client,
        cast(Any, Skills()),
        scope=person_scope,
        tenant_id=tenant.id,
        provider="openai",
        account_scope_id="project",
        pins=(SkillRef(id="skill_a", version="1"),),
        record=record,
    )
    assert (copies, losses) == ((SkillRef(id="copy-a", version="1"),), ()), (
        "the copy uploads without the Managed Agents original"
    )
    assert uploaded == [("SKILL.md",)], f"the catalog bundle's files upload: {uploaded}"

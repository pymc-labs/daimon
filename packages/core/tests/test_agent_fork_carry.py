"""A copy of an agent takes its provider, fleet setup and memory, and names what it lacks."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core import agent_fork, memory_resource
from daimon.core.agent_fork import AgentCopy, CopyCarry, CopyCarryScope, copy_agent, fork_notes
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.authz import Subject
from daimon.core.channel_backend import (
    AgentBackendService,
    BackendActor,
    BackendAgent,
    CarriedBackend,
)
from daimon.core.channel_skills import platform_skill_refs
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_CHANNEL_COPY,
    MA_METADATA_KEY_COPY_OF,
    MA_METADATA_KEY_MANAGED,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.memory_resource import CopiedMemory, ensure_memory_store_and_mount
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import write_memory
from daimon.core.neutral_resources import AgentFleetGrant, WorkerLimits
from daimon.core.specs import SkillRef
from daimon.core.stores import agent_backends, security_audit
from daimon.core.stores.agent_memory_stores import get_memory_store_id
from daimon.core.stores.domain import AgentBackendRevision
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.contracts.ids import ModelRef
from mux.profiles import backend_selection
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    MARouter,
    build_fake_anthropic,
    combine_handlers,
    json_body,
    make_fake_memory_store_handler,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_agent_backend import enable
from .test_agent_backend import owned as owned

NOW = datetime(2026, 10, 13, 21, 0, tzinfo=UTC)
SECRET_URL = "https://crm.example.com/s/sk_SECRET_PATH/mcp?api_key=SECRET_QUERY"
CONSENT = {"declared_drops": ["memory"], "source_pin": "a" * 64, "target_digest": "b" * 64}


def _grant(deadline: datetime, *, epoch: int = 0) -> AgentFleetGrant:
    return AgentFleetGrant(
        epoch=epoch,
        max_workers=4,
        choices=(
            (backend_selection("anthropic"), ModelRef(provider="anthropic", id="claude-haiku-5-5")),
        ),
        limits=WorkerLimits(budget_usd=Decimal("0.9"), deadline=deadline, max_turns=1),
        max_run_seconds=600,
        max_worker_seconds=120,
        max_root_turns=3,
    )


async def _source_setting(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    backend: Any = "gemini",
    model: str | None = "gemini-4-argon",
    grant: AgentFleetGrant | None = None,
    consent: dict[str, Any] | None = None,
) -> AgentBackendRevision:
    async with sessions.begin() as session:
        return await agent_backends.put_revision(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            agent_name="source",
            local=1,
            backend=backend,
            model=model,
            previous_local=None,
            preview_digest="preview",
            digest="source-digest",
            fleet_grant=grant.model_dump(mode="json") if grant is not None else None,
            drop_consent=consent,
        )


def _copy(agent: BackendAgent) -> BackendAgent:
    return agent.model_copy(update={"agent_id": uuid.uuid4(), "name": "copy"})


async def test_a_copy_takes_the_provider_model_settings_and_fleet_grant(owned) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    source = await _source_setting(
        sessions, agent.tenant_id, agent.agent_id, grant=_grant(NOW + timedelta(hours=1), epoch=1)
    )
    copy = _copy(agent)
    carried = await service.carry_to_copy(
        source, copy, fleet_grant=True, account_id=actor.account_id, now=NOW
    )
    assert carried.left_backend is None and carried.fleet
    revision = await service.current(copy)
    assert revision is not None and revision == carried.revision
    assert (revision.local, revision.backend, revision.model) == (1, "gemini", "gemini-4-argon")
    grant = await service.read_fleet_grant(copy.tenant_id, copy.agent_id, now=NOW)
    assert grant is not None and grant.epoch == 1 and grant.max_workers == 4
    async with sessions() as session:
        rows = await security_audit.list_events(
            session, tenant_id=copy.tenant_id, agent_id=copy.agent_id
        )
    assert [(r.operation, r.account_id) for r in rows] == [("copy", actor.account_id)]
    # Never written twice: a copy that already has a setting keeps it.
    again = await service.carry_to_copy(
        source, copy, fleet_grant=True, account_id=actor.account_id, now=NOW
    )
    assert again == CarriedBackend(None, None, fleet=False)


async def test_a_channel_copy_and_an_expired_grant_take_no_fleet(owned) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    source = await _source_setting(
        sessions, agent.tenant_id, agent.agent_id, grant=_grant(NOW + timedelta(hours=1), epoch=1)
    )
    channel_copy = _copy(agent)
    carried = await service.carry_to_copy(
        source, channel_copy, fleet_grant=False, account_id=None, now=NOW
    )
    assert carried.revision is not None and carried.revision.backend == "gemini"
    assert not carried.fleet and carried.revision.fleet_grant is None
    late = _copy(agent)
    expired = await service.carry_to_copy(
        source, late, fleet_grant=True, account_id=None, now=NOW + timedelta(days=1)
    )
    assert expired.revision is not None and not expired.fleet


async def test_a_switch_that_dropped_features_needs_the_copys_own_consent(owned) -> None:
    service, agent, _actor, sessions = owned
    source = await _source_setting(sessions, agent.tenant_id, agent.agent_id, consent=CONSENT)
    # No capture of the copy: its provider stays the default and is named.
    plain = _copy(agent)
    carried = await service.carry_to_copy(source, plain, fleet_grant=True, account_id=None, now=NOW)
    assert carried == CarriedBackend(None, "gemini", fleet=False, left_reason="needs_consent")
    assert await service.current(plain) is None


@pytest.mark.parametrize(
    ("drops", "kept"), [(["memory"], True), ([], True), (["memory", "sandbox"], False)]
)
async def test_the_copys_consent_is_kept_only_within_what_the_source_accepted(
    owned, monkeypatch: pytest.MonkeyPatch, drops: list[str], kept: bool
) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    source = await _source_setting(sessions, agent.tenant_id, agent.agent_id, consent=CONSENT)

    async def capture(target: BackendAgent) -> BackendAgent:
        return target

    service = AgentBackendService(sessions, enabled=True, capture_source=capture)
    plain_service = AgentBackendService(sessions, enabled=True)

    async def preview(session: AsyncSession, target: BackendAgent, *args: Any) -> Any:
        # The copy's own capture, as `_preview` makes it: consent pinned to the copy.
        plain = await plain_service._preview(session, target, *args)  # pyright: ignore[reportPrivateUsage]
        consent = {**CONSENT, "declared_drops": drops, "source_pin": "c" * 64}
        return type(plain).model_validate({**plain.model_dump(), "drop_consent": consent})

    monkeypatch.setattr(service, "_preview", preview)
    copy = _copy(agent)
    carried = await service.carry_to_copy(source, copy, fleet_grant=True, account_id=None, now=NOW)
    if kept:
        assert carried.revision is not None and carried.left_backend is None
        assert carried.revision.drop_consent is not None
        assert carried.revision.drop_consent.source_pin == "c" * 64, "pinned to the copy"
        assert carried.revision.declared_drops == tuple(drops)
    else:
        assert carried == CarriedBackend(None, "gemini", fleet=False, left_reason="needs_consent")


def _client(
    tenant_id: uuid.UUID,
    source: Any,
    created: list[dict[str, Any]],
    memory: FakeMemoryStoreState,
) -> Any:
    def on_create(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json_body(request)
        created.append(body)
        made = ma_agent(
            id="ag_new", name=body["name"], tenant_id=tenant_id, metadata=body["metadata"]
        )
        return httpx.Response(200, json=made.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/([^/]+)",
        lambda _req, _m: httpx.Response(200, json=source.model_dump(mode="json")),
    )
    router.add("POST", r"/v1/agents", on_create)
    return build_fake_anthropic(
        combine_handlers(make_fake_memory_store_handler(memory), router.dispatch)
    )


@pytest.mark.parametrize(
    ("kind", "carry"),
    [
        ("fork", "all"),
        ("fork", "settings"),
        ("fork", None),
        ("channel-copy", "all"),
    ],
    ids=["admin-or-owner", "member", "unsaid", "channel-copy"],
)
async def test_copy_agent_carries_by_who_copies_and_names_what_to_reconnect(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    carry: CopyCarryScope | None,
) -> None:
    """An admin's or owner's copy takes memory and fleet; anyone else's, or one
    whose maker is unknown, takes the settings only; a channel's copy never more."""
    channel_copy = kind == "channel-copy"
    monkeypatch.setattr(agent_fork, "queue_agent_face", lambda *_a, **_k: None)
    factory = db_session_factory
    async with factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    source = ma_agent(
        id="ag_src",
        name="source",
        tenant_id=tenant.id,
        metadata={MA_METADATA_KEY_MANAGED: "true"},
        mcp_servers=[{"type": "url", "name": "crm", "url": SECRET_URL}],
    )
    source_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_src")
    copy_uuid = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_new")
    await save_agent_mcp_credential(
        sessionmaker=factory,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
        tenant_id=tenant.id,
        agent_id=source_uuid,
        mcp_server_url=SECRET_URL,
        plaintext_token="tok_crm",
    )
    service = AgentBackendService(factory, enabled=True)
    await enable(
        service,
        BackendAgent(
            tenant_id=tenant.id, agent_id=source_uuid, name="source", configuration_digest="x"
        ),
        BackendActor(tenant.id, account.id, Subject(is_admin=True)),
    )
    await _source_setting(
        factory,
        tenant.id,
        source_uuid,
        grant=_grant(datetime.now(UTC) + timedelta(hours=1), epoch=1),
    )
    memory = FakeMemoryStoreState()
    created: list[dict[str, Any]] = []
    client = _client(tenant.id, source, created, memory)
    mount = await ensure_memory_store_and_mount(
        client, factory, tenant_id=tenant.id, agent_id=source_uuid, agent_name="source"
    )
    scope = resource_scope(tenant_id=str(tenant.id))
    for path, content in (("/conventions.md", "tabs"), ("/project/facts.md", "due friday")):
        await write_memory(client, mount["memory_store_id"], path, content, scope=scope)

    copy = await copy_agent(
        client,
        factory,
        tenant_id=tenant.id,
        source=source,
        new_name="mine",
        public_url=None,
        subject=Subject(is_admin=True),
        default_agent_name="daimon",
        extra_metadata={MA_METADATA_KEY_CHANNEL_COPY: "chan"} if channel_copy else None,
        **({"carry": carry} if carry is not None else {}),
    )

    assert created[0]["metadata"][MA_METADATA_KEY_COPY_OF] == "source"
    assert not any(server["name"] == "crm" for server in created[0]["mcp_servers"])
    assert copy.carry.reconnect == (("crm", "https://crm.example.com"),)
    async with factory() as session:
        revision = await agent_backends.current(session, tenant.id, copy_uuid)
        store = await get_memory_store_id(session, tenant_id=tenant.id, agent_id=copy_uuid)
    assert revision is not None and (revision.backend, revision.model) == (
        "gemini",
        "gemini-4-argon",
    )
    notes = fork_notes(copy, "mine")
    assert (
        'crm (https://crm.example.com) needs connecting again: ask in chat "connect crm to mine".'
    ) in notes
    assert not any("SECRET" in note for note in notes), "a server's path or query is never echoed"
    kept_back = "source's memory and fleet setup stay with source."
    if channel_copy or carry != "all":
        assert revision.fleet_grant is None and store is None
        assert copy.carry.memory is None
        assert (kept_back in notes) is not channel_copy
        assert not any("fleet setup, with" in note for note in notes)
        return
    assert kept_back not in notes
    assert revision.fleet_grant is not None
    assert store is not None and store != mount["memory_store_id"]
    copied = {(m["path"], m["content"]) for m in memory.memories[store]}
    assert copied == {("/conventions.md", "tabs"), ("/project/facts.md", "due friday")}
    assert copy.carry.memory == CopiedMemory(2, 0)
    assert "It runs on Gemini (gemini-4-argon), like source." in notes
    assert (
        "It has source's fleet setup, with the same limits; each agent spends up to them on "
        "its own."
    ) in notes


def test_fork_notes_name_a_provider_left_behind_and_lost_memory() -> None:
    copy = AgentCopy(
        ma_agent(id="ag_new", name="mine"),
        (),
        carry=CopyCarry(
            source_name="source",
            backend=CarriedBackend(None, "openai", fleet=False, left_reason="needs_consent"),
            memory_failed=True,
            github_repos=("acme/app",),
        ),
    )
    assert fork_notes(copy, "mine") == (
        "source runs on OpenAI, but mine starts on Anthropic: the switch needs confirming "
        'for it. To move it, ask in chat "switch mine to OpenAI".',
        "source's memory couldn't be copied, so mine starts with none.",
        "GitHub access isn't copied (acme/app). To give it a repo, ask in chat "
        '"give mine access to <repo>".',
    )


@pytest.mark.parametrize(
    ("memory", "line"),
    [
        (
            CopiedMemory(3, left=2),
            "Only 3 of source's 5 memory notes were copied. A copy takes up to 500.",
        ),
        (CopiedMemory(3, failed=1), "Only 3 of source's 4 memory notes were copied."),
        (
            CopiedMemory(0, failed=2),
            "source's memory couldn't be copied, so mine starts with none.",
        ),
    ],
)
def test_fork_notes_say_how_much_memory_came(memory: CopiedMemory, line: str) -> None:
    copy = AgentCopy(
        ma_agent(id="ag_new", name="mine"), (), carry=CopyCarry(source_name="source", memory=memory)
    )
    assert line in fork_notes(copy, "mine")


def test_a_copy_of_a_seeded_agent_keeps_its_platform_skills() -> None:
    ref = SkillRef(type="custom", skill_id="docx", platforms=("teams",))
    skills = {"daimon": (ref,)}
    copy = ma_agent(name="mine", metadata={MA_METADATA_KEY_COPY_OF: "daimon"})
    other = ma_agent(name="daimon")
    assert platform_skill_refs(copy, "teams", skills) == (ref,)
    assert platform_skill_refs(copy, "slack", skills) == ()
    assert platform_skill_refs(other, "teams", skills) == (), "a name alone never qualifies"


async def test_a_provider_turned_off_here_is_left_behind(owned) -> None:
    service, agent, _actor, sessions = owned
    source = await _source_setting(sessions, agent.tenant_id, agent.agent_id)
    copy = _copy(agent)
    carried = await service.carry_to_copy(source, copy, fleet_grant=True, account_id=None, now=NOW)
    assert carried == CarriedBackend(None, "gemini", fleet=False, left_reason="turned_off")
    assert await service.current(copy) is None


@pytest.mark.parametrize("problem", ["unreadable", "provider-off"])
async def test_a_grant_that_fails_its_checks_is_left_behind_and_named(owned, problem: str) -> None:
    service, agent, _actor, sessions = owned
    if problem == "unreadable":
        # Epoch 0 on revision 1 reads back as an invalid grant.
        value = _grant(NOW + timedelta(hours=1))
    else:
        value = AgentFleetGrant(
            epoch=1,
            max_workers=2,
            choices=((backend_selection("openai"), ModelRef(provider="openai", id="gpt-6-luna")),),
            limits=WorkerLimits(
                budget_usd=Decimal("0.9"), deadline=NOW + timedelta(hours=1), max_turns=1
            ),
            max_run_seconds=600,
            max_worker_seconds=120,
            max_root_turns=3,
        )
    source = await _source_setting(
        sessions, agent.tenant_id, agent.agent_id, backend=None, model=None, grant=value
    )
    copy = _copy(agent)
    carried = await service.carry_to_copy(source, copy, fleet_grant=True, account_id=None, now=NOW)
    assert carried == CarriedBackend(None, None, fleet=False, fleet_left=True)
    notes = fork_notes(
        AgentCopy(copy_ma(), (), carry=CopyCarry(source_name="source", backend=carried)), "mine"
    )
    assert notes == ("source's fleet setup can't be used here any more, so mine has none.",)


def copy_ma() -> Any:
    return ma_agent(id="ag_new", name="mine")


@pytest.mark.parametrize(
    ("reason", "line"),
    [
        (
            "turned_off",
            "source runs on Gemini, which is turned off here, so mine runs on Anthropic.",
        ),
        (
            "not_runnable",
            "source's Gemini setting can't run here any more, so mine runs on Anthropic.",
        ),
        (
            "needs_consent",
            "source runs on Gemini, but mine starts on Anthropic: the switch needs confirming "
            'for it. To move it, ask in chat "switch mine to Gemini".',
        ),
    ],
)
def test_fork_notes_give_the_true_reason_a_provider_was_left(reason: Any, line: str) -> None:
    backend = CarriedBackend(None, "gemini", fleet=False, left_reason=reason)
    notes = fork_notes(
        AgentCopy(copy_ma(), (), carry=CopyCarry(source_name="source", backend=backend)), "mine"
    )
    assert notes == (line,)


async def _seeded_store(
    client: Any, factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, count: int
) -> uuid.UUID:
    source_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_src")
    mount = await ensure_memory_store_and_mount(
        client, factory, tenant_id=tenant_id, agent_id=source_uuid, agent_name="source"
    )
    scope = resource_scope(tenant_id=str(tenant_id))
    for n in range(count):
        await write_memory(client, mount["memory_store_id"], f"/n{n}.md", str(n), scope=scope)
    return source_uuid


async def test_memory_past_the_limit_and_failed_notes_are_counted(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    source_uuid = await _seeded_store(client, db_session_factory, tenant.id, 4)
    real_write = memory_resource.write_memory

    async def flaky_write(client: Any, store: str, path: str, content: str, **kw: Any) -> None:
        if path == "/n1.md":
            raise RuntimeError("provider hiccup")
        await real_write(client, store, path, content, **kw)

    monkeypatch.setattr(memory_resource, "COPY_MEMORY_LIMIT", 3)
    monkeypatch.setattr(memory_resource, "write_memory", flaky_write)
    copied = await memory_resource.copy_shared_memory(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        source_agent_id=source_uuid,
        copy_agent_id=uuid.uuid4(),
        copy_name="mine",
    )
    assert copied == CopiedMemory(2, left=1, failed=1)


def _plain_client(tenant_id: uuid.UUID, source: Any, created: list[dict[str, Any]]) -> Any:
    return _client(tenant_id, source, created, FakeMemoryStoreState())


@pytest.mark.parametrize(
    ("metadata", "stamp"),
    [({MA_METADATA_KEY_COPY_OF: "daimon"}, "daimon"), ({}, None)],
    ids=["copy-of-a-copy", "plain-source"],
)
async def test_the_seeded_name_follows_copies_of_copies_only(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, str],
    stamp: str | None,
) -> None:
    monkeypatch.setattr(agent_fork, "queue_agent_face", lambda *_a, **_k: None)
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
    source = ma_agent(id="ag_src", name="source", tenant_id=tenant.id, metadata=metadata)
    created: list[dict[str, Any]] = []
    await copy_agent(
        _plain_client(tenant.id, source, created),
        db_session_factory,
        tenant_id=tenant.id,
        source=source,
        new_name="mine",
        public_url=None,
        subject=Subject(is_admin=True),
        default_agent_name="daimon",
    )
    assert created[0]["metadata"].get(MA_METADATA_KEY_COPY_OF) == stamp


async def test_a_failed_carry_still_returns_the_copy_and_lists_only_active_repos(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_fork, "queue_agent_face", lambda *_a, **_k: None)
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise SQLAlchemyError("database went away")

    async def repos(*_a: Any, **_k: Any) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(full_name="acme/app", status="active"),
            SimpleNamespace(full_name="acme/old", status="revoked"),
        ]

    monkeypatch.setattr(agent_fork, "copy_shared_memory", broken)
    monkeypatch.setattr(agent_fork.agent_backends, "current", broken)
    monkeypatch.setattr(agent_fork.github_access, "list_agent_repos", repos)
    source = ma_agent(id="ag_src", name="source", tenant_id=tenant.id)
    copy = await copy_agent(
        _plain_client(tenant.id, source, []),
        db_session_factory,
        tenant_id=tenant.id,
        source=source,
        new_name="mine",
        public_url=None,
        subject=Subject(is_admin=True),
        default_agent_name="daimon",
        carry="all",
    )
    assert copy.agent.id == "ag_new"
    assert copy.carry.memory_failed
    assert copy.carry.backend is None
    assert copy.carry.github_repos == ("acme/app",)

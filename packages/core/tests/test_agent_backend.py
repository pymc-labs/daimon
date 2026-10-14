"""Backend preferences use real policy, immutable revisions and scoped audit rows."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    BackendActor,
    BackendAgent,
    BackendUnsupported,
    CompatibilityItem,
    TenantBackendPolicy,
    check_backend,
    clear_channel_backend,
    compatibility_preview,
    current_agent_backend,
    current_backend,
    make_backend_service,
    resolve_turn_backend,
    set_channel_backend,
)
from daimon.core.config import TurnSettings
from daimon.core.stores import access_policy, mux_state, security_audit
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ChannelRef
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
async def owned(
    db_preflight: DatabasePreflight, db_nullpool_engine: AsyncEngine
) -> tuple[AgentBackendService, BackendAgent, BackendActor, async_sessionmaker[AsyncSession]]:
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    agent = BackendAgent(
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        name="helper",
        configuration_digest="actual-config",
    )
    actor = BackendActor(tenant.id, account.id, Subject(is_admin=True))
    service = AgentBackendService(sessions, enabled=True)
    return service, agent, actor, sessions


async def enable(service: AgentBackendService, agent: BackendAgent, actor: BackendActor) -> None:
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(
        agent.tenant_id, ("anthropic", "openai", "gemini"), policy.revision, actor
    )


def test_preview_reports_unknown_and_unsupported_from_profile_metadata() -> None:
    agent = BackendAgent(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="helper",
        configuration_digest="actual",
        shared_threads=True,
        skills=(
            CompatibilityItem(
                name="native-pin", requires=("skills_bundle",), providers=("anthropic",)
            ),
        ),
        mcps=(CompatibilityItem(name="private-server", requires=("vaults",)),),
        required_capabilities=("native_schedules", "workspace_export_import"),
    )
    policy = TenantBackendPolicy(tenant_id=agent.tenant_id, revision="policy")
    preview = compatibility_preview(
        agent, "gemini", policy, cost_line="Existing list price; turn maximum unknown"
    )
    assert not preview.allowed
    assert preview.unsupported_skills == ("native-pin",)
    assert preview.unsupported_mcps == ("private-server",)
    assert preview.unsupported_capabilities == ("native_schedules", "workspace_export_import")
    assert preview.shared_thread_limits
    assert (
        preview.digest
        == compatibility_preview(agent, "gemini", policy, cost_line=preview.cost_line).digest
    )
    assert (
        preview.digest
        != compatibility_preview(
            agent.model_copy(update={"configuration_digest": "changed"}),
            "gemini",
            policy,
            cost_line=preview.cost_line,
        ).digest
    )
    assert (
        preview.digest
        != compatibility_preview(agent, "gemini", policy, cost_line="Changed rate").digest
    )
    assert (
        preview.digest
        != compatibility_preview(
            agent, "gemini", policy, setting_revision=1, cost_line=preview.cost_line
        ).digest
    )


async def test_default_and_disabled_resolution_perform_no_reads() -> None:
    class NoDatabase:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("disabled resolution accessed a database")

    channel = ChannelRef(tenant_id=str(uuid.uuid4()), platform="discord", channel_id="chan")
    assert await resolve_turn_backend(NoDatabase(), channel, "helper", enabled=False) == (
        None,
        False,
        "default",
    )
    assert await resolve_turn_backend(
        NoDatabase(), channel, "helper", enabled=True, retained_revision=0
    ) == (None, False, "default")


async def test_set_clear_revert_and_independent_audit_readback(owned) -> None:
    service, agent, actor, sessions = owned
    assert await service.current(agent) is None
    policy = await service.tenant_policy(agent.tenant_id)
    assert policy.enabled_backends == ("anthropic",)
    with pytest.raises(AgentBackendRefused, match="backend not allowed"):
        await service.set_backend(agent, "openai", 0, actor)
    await enable(service, agent, actor)
    preview = await service.preview(agent, "openai")
    first = await service.set_backend(
        agent, "openai", 0, actor, expected_preview_digest=preview.digest
    )
    second = await service.clear_backend(agent, 1, actor)
    third = await service.revert(agent, actor, expected_revision=2)
    assert [first.local, second.local, third.local] == [1, 2, 3]
    assert [first.backend, second.backend, third.backend] == ["openai", None, "openai"]
    assert await service.revision(agent, 1) == first
    async with sessions() as session:
        rows = await security_audit.list_events(session, tenant_id=agent.tenant_id)
    assert [r.operation for r in rows] == ["allowlist", "set", "clear", "revert"]
    assert [r.scope for r in rows[1:]] == [f"agent_backend:{agent.agent_id}:{n}" for n in (1, 2, 3)]
    assert all(r.account_id == actor.account_id for r in rows)


async def test_stale_preview_rejects_policy_agent_and_price_replacements(owned) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    preview = await service.preview(agent, "openai")
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await service.set_backend(
            agent.model_copy(update={"configuration_digest": "changed"}),
            "openai",
            0,
            actor,
            expected_preview_digest=preview.digest,
        )
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(
        agent.tenant_id, ("anthropic", "openai"), policy.revision, actor
    )
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await service.set_backend(agent, "openai", 0, actor, expected_preview_digest=preview.digest)
    assert await service.current(agent) is None
    line = {"value": "existing rate snapshot"}
    priced = AgentBackendService(sessions, enabled=True, cost_line=lambda a, b: line["value"])
    quote = await priced.preview(agent, "openai")
    line["value"] = "changed rate snapshot"
    with pytest.raises(AgentBackendRefused, match="stale preview"):
        await priced.set_backend(agent, "openai", 0, actor, expected_preview_digest=quote.digest)


@pytest.mark.parametrize(
    "subject",
    [
        Subject(),
        Subject(is_admin=True, via_agent_key=True),
        Subject(administered_channel_ids=frozenset({"chan"})),
    ],
)
async def test_non_admin_and_agent_key_refused_without_revision_or_audit(owned, subject) -> None:
    service, agent, actor, _ = owned
    with pytest.raises(AgentBackendRefused, match="admin required"):
        await service.set_backend(agent, "anthropic", 0, replace(actor, subject=subject))
    assert await service.current(agent) is None
    assert await service.audit(agent.tenant_id) == ()


async def test_foreign_tenant_and_account_refused(owned) -> None:
    service, agent, actor, sessions = owned
    async with sessions.begin() as session:
        foreign = await make_account(session)
    with pytest.raises(AgentBackendRefused, match="tenant mismatch"):
        await service.set_backend(
            agent, "anthropic", 0, replace(actor, tenant_id=foreign.tenant_id)
        )
    with pytest.raises(AgentBackendRefused, match="actor not found"):
        await service.set_backend(agent, "anthropic", 0, replace(actor, account_id=foreign.id))
    assert await service.current(agent) is None


async def test_concurrent_cas_has_one_winner_and_one_audit(owned) -> None:
    service, agent, actor, _ = owned
    results = await asyncio.gather(
        service.set_backend(agent, "anthropic", 0, actor),
        service.set_backend(agent, "anthropic", 0, actor),
        return_exceptions=True,
    )
    assert (
        sum(isinstance(r, AgentBackendRefused) and r.reason == "stale_revision" for r in results)
        == 1
    )
    assert (await service.current(agent)).local == 1
    assert len(await service.audit(agent.tenant_id)) == 1


async def test_audit_failure_rolls_back_setting(owned, monkeypatch) -> None:
    service, agent, actor, _ = owned

    async def unavailable(*args, **kwargs):
        raise RuntimeError("offline audit failure")

    monkeypatch.setattr(security_audit, "append_event", unavailable)
    with pytest.raises(RuntimeError, match="audit failure"):
        await service.set_backend(agent, "anthropic", 0, actor)
    assert await service.current(agent) is None


async def test_agent_effective_revisions_do_not_become_channel_overrides(owned) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, "openai", 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="chan")
    async with sessions.begin() as session:
        first, shared, source = await resolve_turn_backend(session, channel, agent, enabled=True)
        assert first is not None and source == "agent" and not shared
    async with sessions.begin() as session:
        assert await current_backend(session, channel) is None
        repeated, _, _ = await resolve_turn_backend(session, channel, agent, enabled=True)
        assert repeated == first
        assert await mux_state.put_config_revision(session, first) == first
        assert await current_backend(session, channel) is None
    await service.set_backend(agent, "gemini", 1, actor)
    async with sessions.begin() as session:
        second, _, _ = await resolve_turn_backend(session, channel, agent, enabled=True)
        assert second is not None and second.local > first.local
        assert (
            await resolve_turn_backend(
                session, channel, agent, enabled=True, retained_revision=first.local
            )
        )[0] == first
        explicit = await set_channel_backend(session, channel, BackendConfig())
        assert explicit.local > second.local
        assert (await resolve_turn_backend(session, channel, agent, enabled=True))[0] == explicit


async def test_disallowed_retained_backend_is_refused_by_same_admission_gate(owned) -> None:
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, "openai", 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="chan")
    async with sessions.begin() as session:
        revision, _, _ = await resolve_turn_backend(session, channel, agent, enabled=True)
    assert revision is not None
    with pytest.raises(BackendUnsupported):
        check_backend(revision, enabled_backends=("anthropic",))


def test_factory_uses_existing_flags() -> None:
    assert not make_backend_service(None, TurnSettings()).enabled
    assert not make_backend_service(None, TurnSettings(path="mux")).enabled
    assert make_backend_service(None, TurnSettings(path="mux", channel_backends=True)).enabled


async def test_public_loader_uses_actual_scoped_sdk_projection_and_never_updates(owned) -> None:
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.testing.ma import list_response
    from daimon.testing.ma_models import ma_agent
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    _, agent, _, sessions = owned
    native = ma_agent(
        id="agent-owned", tenant_id=str(agent.tenant_id), name=agent.name, model="claude-opus-5-5"
    )
    payload = native.model_dump(mode="json")
    payload["metadata"]["daimon_name"] = agent.name
    trace = ScriptedTransport()
    trace.queue(ScriptedReply("GET", "/v1/agents", list_response([payload])))
    async with trace.client() as client:
        loaded = await load_backend_agent(
            client, sessions, tenant_id=agent.tenant_id, agent_name=agent.name
        )
    assert loaded.agent_id == derive_agent_uuid(
        tenant_id=agent.tenant_id, ma_agent_id="agent-owned"
    )
    assert loaded.model == "claude-opus-5-5" and loaded.configuration_digest
    assert [(r.method, r.path) for r in trace.requests] == [("GET", "/v1/agents")]
    assert not trace.violations and not trace.replies


async def test_audit_filters_before_pagination_and_unknown_accounts_are_not_accepted(owned) -> None:
    service, agent, actor, sessions = owned
    neighbor = agent.model_copy(update={"agent_id": uuid.uuid4(), "name": "neighbor"})
    await service.set_backend(neighbor, "anthropic", 0, actor)
    first = await service.set_backend(agent, "anthropic", 0, actor)
    rows = await service.audit(agent.tenant_id, agent_id=agent.agent_id, limit=1)
    assert len(rows) == 1 and rows[0].scope == f"agent_backend:{agent.agent_id}:{first.local}"
    assert await service.audit(uuid.uuid4(), agent_id=agent.agent_id) == ()
    with pytest.raises(AgentBackendRefused, match="actor not found"):
        await service.set_backend(agent, "anthropic", 1, replace(actor, account_id=uuid.uuid4()))


async def test_factory_preserves_actual_agent_model_and_unknown_native_rates(owned) -> None:
    _, agent, _, sessions = owned
    settings = TurnSettings(path="mux", channel_backends=True)
    service = make_backend_service(sessions, settings)
    opus = await service.preview(agent.model_copy(update={"model": "claude-opus-5-5"}), "anthropic")
    assert "claude-opus-5-5" in opus.cost_line
    assert "haiku" not in opus.cost_line.lower()
    native = await service.preview(agent, "openai")
    assert "unknown" in native.cost_line.lower() and "worst" in native.cost_line.lower()


async def test_default_revision_reput_does_not_become_override_or_hide_agent_setting(owned) -> None:
    from mux.contracts.config import ConfigRevision, resolve_default

    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, "openai", 0, actor)
    channel = ChannelRef(
        tenant_id=str(agent.tenant_id), platform="discord", channel_id="default-pin"
    )
    default = ConfigRevision.create(channel, 0, resolve_default(None))
    async with sessions.begin() as session:
        await mux_state.put_config_revision(session, default)
        assert await current_backend(session, channel) is None
        assert await mux_state.latest_config_revision(session, channel) == default
        effective, _, source = await resolve_turn_backend(session, channel, agent, enabled=True)
        assert effective is not None and source == "agent" and effective.backend == "openai"
    async with sessions() as session:
        assert await resolve_turn_backend(
            session, channel, agent, enabled=True, retained_revision=0
        ) == (None, False, "default")


@pytest.fixture(scope="module")
def prior_reader():
    # Exact access_policy.py from the shared interface's parent commit.
    path = Path(__file__).parent / "fixtures/access_policy_prior.py"
    spec = importlib.util.spec_from_file_location("prior_access_policy_reader", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module.TenantAccessPolicy
    del sys.modules[spec.name]


async def configure_nondefault(owned):
    service, agent, actor, sessions = owned
    restrictive = TenantAccessPolicy(
        invoker_user_ids=("member",),
        channel_rules={"restricted": ChannelRule(readers="inside", writers="none")},
        agent_rules={"helper": AgentRule(runs_in=("restricted",))},
    )
    async with sessions.begin() as session:
        await access_policy.set_access_policy(
            session, tenant_id=agent.tenant_id, policy=restrictive
        )
    async with sessions() as session:
        before = await session.scalar(
            text("SELECT policy::text FROM tenant_access_policies WHERE tenant_id=:tenant"),
            {"tenant": agent.tenant_id},
        )
    policy = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(
        agent.tenant_id, ("anthropic", "openai", "gemini"), policy.revision, actor
    )
    return restrictive, before


async def assert_prior_read(sessions, tenant_id, prior_reader, restrictive, before):
    async with sessions() as session:
        payload, encoded = (
            await session.execute(
                text(
                    "SELECT policy, policy::text FROM tenant_access_policies WHERE tenant_id=:tenant"
                ),
                {"tenant": tenant_id},
            )
        ).one()
    assert "enabled_backends" not in payload
    assert encoded == before
    prior = prior_reader.model_validate(payload)
    assert prior.invoker_user_ids == restrictive.invoker_user_ids
    assert prior.channel_rules["restricted"].writers == "none"
    assert prior.agent_rules["helper"].runs_in == ("restricted",)
    return payload


async def test_rolling_prior_reader_accepts_real_nondefault_allowlist_write(owned, prior_reader):
    service, agent, _, sessions = owned
    restrictive, before = await configure_nondefault(owned)
    await assert_prior_read(sessions, agent.tenant_id, prior_reader, restrictive, before)
    async with sessions() as session:
        actual = await access_policy.load_access_policy(session, tenant_id=agent.tenant_id)
    assert actual.enabled_backends == ("anthropic", "openai", "gemini")
    assert actual.channel_rules == restrictive.channel_rules
    assert (
        await service.tenant_policy(agent.tenant_id)
    ).enabled_backends == actual.enabled_backends


@pytest.mark.fresh_schema
async def test_downgrade_prior_reader_accepts_real_nondefault_allowlist_write(owned, prior_reader):
    _, agent, _, sessions = owned
    restrictive, before = await configure_nondefault(owned)
    await assert_prior_read(sessions, agent.tenant_id, prior_reader, restrictive, before)
    path = Path(__file__).parents[1] / "alembic/versions/0091_agent_backend_settings.py"
    spec = importlib.util.spec_from_file_location("backend_settings_rollback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with sessions.begin() as session:
        connection = await session.connection()

        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.downgrade()
            schema = sync.scalar(text("SELECT current_schema()"))
            assert "enabled_backends" not in {
                c["name"]
                for c in inspect(sync).get_columns("tenant_access_policies", schema=schema)
            }

        await connection.run_sync(downgrade)
    try:
        await assert_prior_read(sessions, agent.tenant_id, prior_reader, restrictive, before)
    finally:
        async with sessions.begin() as session:
            connection = await session.connection()

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    module.upgrade()
                    fleet_path = path.with_name("0096_agent_fleet_grant.py")
                    fleet_spec = importlib.util.spec_from_file_location(
                        "fleet_grant_restore", fleet_path
                    )
                    assert fleet_spec is not None and fleet_spec.loader is not None
                    fleet_module = importlib.util.module_from_spec(fleet_spec)
                    fleet_spec.loader.exec_module(fleet_module)
                    fleet_module.upgrade()

            await connection.run_sync(upgrade)
    async with sessions() as session:
        restored = await access_policy.load_access_policy(session, tenant_id=agent.tenant_id)
    assert restored.enabled_backends == ("anthropic",)
    assert restored.channel_rules == restrictive.channel_rules
    assert restored.agent_rules == restrictive.agent_rules


@pytest.mark.parametrize(
    "stored", [[], ["openai"], ["anthropic", "bogus"], ["anthropic", "anthropic"]]
)
async def test_invalid_allowlist_column_refuses_without_policy_fallback(owned, stored):
    _, agent, _, sessions = owned
    await configure_nondefault(owned)
    async with sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE tenant_access_policies SET enabled_backends=CAST(:backends AS jsonb) WHERE tenant_id=:tenant"
            ),
            {"tenant": agent.tenant_id, "backends": json.dumps(stored)},
        )
    async with sessions() as session:
        with pytest.raises(access_policy.AccessPolicyUnreadable):
            await access_policy.load_access_policy(session, tenant_id=agent.tenant_id)


async def test_recreated_agent_resolves_by_scoped_identity_without_old_name_inheritance(owned):
    service, old, actor, sessions = owned
    await enable(service, old, actor)
    await service.set_backend(old, "openai", 0, actor)
    replacement = old.model_copy(update={"agent_id": uuid.uuid4()})
    channel = ChannelRef(tenant_id=str(old.tenant_id), platform="discord", channel_id="recreated")
    async with sessions.begin() as session:
        assert await resolve_turn_backend(session, channel, replacement, enabled=True) == (
            None,
            False,
            "default",
        )
    await service.set_backend(replacement, "gemini", 0, actor)
    async with sessions.begin() as session:
        revision, _, source = await resolve_turn_backend(
            session, channel, replacement, enabled=True
        )
        assert source == "agent" and revision.backend == "gemini"
        with pytest.raises(AgentBackendRefused, match="agent identity required"):
            await resolve_turn_backend(session, channel, old.name, enabled=True)
        assert (
            await current_agent_backend(session, old.tenant_id, replacement.agent_id)
        ).agent_id == replacement.agent_id
    async with sessions.begin() as session:
        revision, _, source = await resolve_turn_backend(session, channel, old, enabled=True)
        assert source == "agent" and revision.backend == "openai"


async def test_identity_lookup_retains_renamed_clear_and_ignores_same_name_replacement(owned):
    service, old, actor, sessions = owned
    await enable(service, old, actor)
    await service.set_backend(old, "openai", 0, actor)
    renamed = old.model_copy(update={"name": "renamed"})
    cleared = await service.clear_backend(renamed, 1, actor)
    replacement = old.model_copy(update={"agent_id": uuid.uuid4()})
    await service.set_backend(replacement, "gemini", 0, actor)
    channel = ChannelRef(tenant_id=str(old.tenant_id), platform="discord", channel_id="renamed")
    async with sessions.begin() as session:
        assert await current_agent_backend(session, old.tenant_id, old.agent_id) == cleared
        revision, _, source = await resolve_turn_backend(
            session, channel, replacement, enabled=True
        )
        assert source == "agent" and revision.backend == "gemini"
        assert (await resolve_turn_backend(session, channel, renamed, enabled=True))[2] == "default"


async def test_channel_clear_inherits_agent_without_losing_explicit_or_retained_overrides(owned):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, "openai", 0, actor)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="clear")
    async with sessions.begin() as session:
        previous = await set_channel_backend(session, channel, BackendConfig())
        assert (await resolve_turn_backend(session, channel, agent, enabled=True))[2] == "channel"
        cleared = await clear_channel_backend(session, channel)
        assert cleared.local > previous.local
        assert await current_backend(session, channel) == cleared
        assert await clear_channel_backend(session, channel) == cleared
        revision, _, source = await resolve_turn_backend(session, channel, agent, enabled=True)
        assert source == "agent" and revision.backend == "openai"
        assert await mux_state.put_config_revision(session, cleared) == cleared
        assert await mux_state.config_origin(session, channel, cleared.local) == "channel_clear"
        retained, _, source = await resolve_turn_backend(
            session, channel, agent, enabled=True, retained_revision=cleared.local
        )
        assert retained == cleared and source == "channel"
        explicit = await set_channel_backend(session, channel, BackendConfig())
        assert explicit.local > revision.local
        assert (await resolve_turn_backend(session, channel, agent, enabled=True)) == (
            explicit,
            False,
            "channel",
        )
        assert await set_channel_backend(session, channel, BackendConfig()) == explicit
        assert (
            await resolve_turn_backend(
                session, channel, agent, enabled=True, retained_revision=previous.local
            )
        )[0] == previous
        await clear_channel_backend(session, channel)
        assert (await resolve_turn_backend(session, channel, agent, enabled=True))[2] == "agent"


async def test_channel_clear_without_agent_setting_keeps_default_and_sharing_history(owned):
    _, agent, _, sessions = owned
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="history")
    async with sessions.begin() as session:
        shared = await set_channel_backend(session, channel, BackendConfig(thread_mode="shared"))
        cleared = await clear_channel_backend(session, channel)
        assert await resolve_turn_backend(session, channel, agent, enabled=True) == (
            None,
            True,
            "default",
        )
        retained, _, source = await resolve_turn_backend(
            session, channel, agent, enabled=True, retained_revision=shared.local
        )
        assert retained == shared and source == "channel"
        assert await mux_state.get_config_revision(session, channel, cleared.local) == cleared


async def test_concurrent_resolvers_share_read_fence_and_refuse_held_writer_before_io(owned):
    from daimon.core.session_fence_retry import FenceUnavailable

    _, agent, _, sessions = owned
    arrived = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def reader(index):
        channel = ChannelRef(
            tenant_id=str(agent.tenant_id), platform="discord", channel_id=f"concurrent-{index}"
        )
        async with sessions.begin() as session:
            assert await resolve_turn_backend(session, channel, agent, enabled=True) == (
                None,
                False,
                "default",
            )
            arrived[index].set()
            await release.wait()

    tasks = [asyncio.create_task(reader(index)) for index in range(2)]
    try:
        async with asyncio.timeout(10):
            await asyncio.gather(*(event.wait() for event in arrived))
    finally:
        release.set()
        await asyncio.gather(*tasks)
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="writer")
    async with sessions.begin() as writer:
        await access_policy.lock_policy_writes_exclusive(writer, tenant_id=agent.tenant_id)
        async with sessions.begin() as reader_session:
            with pytest.raises(FenceUnavailable):
                await resolve_turn_backend(reader_session, channel, agent, enabled=True)
    async with sessions.begin() as session:
        assert await resolve_turn_backend(session, channel, agent, enabled=True) == (
            None,
            False,
            "default",
        )
        assert await mux_state.latest_config_revision(session, channel) is None


@pytest.mark.parametrize("replacement_backend", ["openai", "gemini"])
async def test_concurrent_materialization_settles_actual_postgres_conflict(
    owned, monkeypatch, replacement_backend
):
    service, first_agent, actor, sessions = owned
    await enable(service, first_agent, actor)
    await service.set_backend(first_agent, "openai", 0, actor)
    second_agent = first_agent.model_copy(update={"agent_id": uuid.uuid4(), "name": "other"})
    await service.set_backend(second_agent, replacement_backend, 0, actor)
    channel = ChannelRef(
        tenant_id=str(first_agent.tenant_id), platform="discord", channel_id="allocation"
    )
    original = mux_state.next_config_local
    candidates = []
    arrived = asyncio.Event()

    async def simultaneous_candidate(session, current_channel):
        candidate = await original(session, current_channel)
        if candidate == 1:
            candidates.append(candidate)
            if len(candidates) == 2:
                arrived.set()
            await arrived.wait()
        return candidate

    monkeypatch.setattr(mux_state, "next_config_local", simultaneous_candidate)

    async def resolve(agent):
        async with sessions.begin() as session:
            return await resolve_turn_backend(session, channel, agent, enabled=True)

    async with asyncio.timeout(15):
        results = await asyncio.gather(resolve(first_agent), resolve(second_agent))
    assert candidates == [1, 1]
    assert [result[2] for result in results] == ["agent", "agent"]
    assert [result[0].backend for result in results] == ["openai", replacement_backend]
    expected = {1} if replacement_backend == "openai" else {1, 2}
    assert {result[0].local for result in results} == expected
    async with sessions() as session:
        for result in results:
            assert (
                await mux_state.get_config_revision(session, channel, result[0].local) == result[0]
            )
            assert await mux_state.config_origin(session, channel, result[0].local) == "agent"


async def test_name_only_and_foreign_identity_requests_refuse_before_database_reads():
    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError("identity refusal accessed a database")

    tenant_id = uuid.uuid4()
    channel = ChannelRef(tenant_id=str(tenant_id), platform="discord", channel_id="identity")
    with pytest.raises(AgentBackendRefused, match="agent identity required"):
        await resolve_turn_backend(NoDatabase(), channel, "helper", enabled=True)
    with pytest.raises(AgentBackendRefused, match="agent identity required"):
        await current_agent_backend(NoDatabase(), tenant_id, "helper")
    foreign = BackendAgent(
        tenant_id=uuid.uuid4(), agent_id=uuid.uuid4(), name="helper", configuration_digest="scoped"
    )
    with pytest.raises(AgentBackendRefused, match="tenant mismatch"):
        await resolve_turn_backend(NoDatabase(), channel, foreign, enabled=True)


async def test_clear_preview_uses_the_same_default_and_digest(owned):
    service, agent, _, _ = owned
    from mux.contracts.config import DEFAULT_BACKEND

    assert await service.preview(agent, None) == await service.preview(agent, DEFAULT_BACKEND)


async def test_scoped_source_reuse_projects_actual_sdk_retrieve_without_extra_request(owned):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.testing.ma_models import ma_agent
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    _, agent, _, sessions = owned
    native = ma_agent(
        id="agent-owned", tenant_id=str(agent.tenant_id), name=agent.name, model="claude-opus-5-5"
    )
    payload = native.model_dump(mode="json")
    payload["metadata"]["daimon_name"] = agent.name
    trace = ScriptedTransport()
    trace.queue(ScriptedReply("GET", "/v1/agents/agent-owned", httpx.Response(200, json=payload)))
    async with trace.client() as client:
        actual = await client.beta.agents.retrieve("agent-owned")
        loaded = await load_backend_agent(
            client, sessions, tenant_id=agent.tenant_id, agent_name=agent.name, actual_agent=actual
        )
    assert loaded.agent_id == derive_agent_uuid(tenant_id=agent.tenant_id, ma_agent_id=actual.id)
    assert loaded.model == "claude-opus-5-5" and loaded.configuration_digest
    assert [(r.method, r.path) for r in trace.requests] == [("GET", "/v1/agents/agent-owned")]
    assert not trace.violations and not trace.replies


@pytest.mark.parametrize("invalid", ["foreign", "missing_tenant", "wrong_name", "archived"])
async def test_scoped_source_reuse_refuses_unbound_or_inactive_source_without_extra_request(
    owned, invalid
):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.testing.ma_models import ma_agent
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    _, agent, _, sessions = owned
    payload = ma_agent(
        id="agent-owned", tenant_id=str(agent.tenant_id), name=agent.name
    ).model_dump(mode="json")
    payload["metadata"]["daimon_name"] = agent.name
    if invalid == "foreign":
        payload["metadata"]["daimon_tenant"] = str(uuid.uuid4())
    elif invalid == "missing_tenant":
        payload["metadata"].pop("daimon_tenant")
    elif invalid == "wrong_name":
        payload["metadata"]["daimon_name"] = "another"
    else:
        payload["archived_at"] = "2026-10-12T00:00:00Z"
    trace = ScriptedTransport()
    trace.queue(ScriptedReply("GET", "/v1/agents/agent-owned", httpx.Response(200, json=payload)))
    async with trace.client() as client:
        actual = await client.beta.agents.retrieve("agent-owned")
        with pytest.raises(AgentBackendRefused):
            await load_backend_agent(
                client,
                sessions,
                tenant_id=agent.tenant_id,
                agent_name=agent.name,
                actual_agent=actual,
            )
    assert [(r.method, r.path) for r in trace.requests] == [("GET", "/v1/agents/agent-owned")]
    assert not trace.violations and not trace.replies


async def test_retained_and_disabled_resolution_need_no_fabricated_source(owned):
    _, agent, _, sessions = owned
    channel = ChannelRef(tenant_id=str(agent.tenant_id), platform="discord", channel_id="no-source")
    async with sessions.begin() as session:
        retained = await set_channel_backend(session, channel, BackendConfig())
    async with sessions.begin() as session:
        assert await resolve_turn_backend(session, channel, None, enabled=False) == (
            None,
            False,
            "default",
        )
        assert await resolve_turn_backend(
            session, channel, None, enabled=True, retained_revision=0
        ) == (None, False, "default")
        result = await resolve_turn_backend(
            session, channel, None, enabled=True, retained_revision=retained.local
        )
        assert result == (retained, False, "channel")
        with pytest.raises(AgentBackendRefused, match="agent identity required"):
            await resolve_turn_backend(session, channel, None, enabled=True)


async def test_source_loader_retains_full_sdk_intent_without_serializing_it(owned):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.testing.ma_models import ma_agent
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    _, prior, _, sessions = owned
    payload = ma_agent(
        id="agent-source", name=prior.name, tenant_id=str(prior.tenant_id)
    ).model_dump(mode="json")
    payload["system"] = "private source guidance"
    payload["skills"] = [{"type": "custom", "skill_id": "skill-original", "version": "17"}]
    payload["mcp_servers"] = [
        {"type": "url", "name": "source-server", "url": "https://mcp.example.test/source"}
    ]
    payload["tools"] = [
        {
            "type": "mcp_toolset",
            "mcp_server_name": "source-server",
            "default_config": {"enabled": True, "permission": "always_ask"},
        }
    ]
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("GET", "/v1/agents/agent-source", httpx.Response(200, json=payload))
    )
    async with transport.client() as client:
        actual = await client.beta.agents.retrieve("agent-source")
        loaded = await load_backend_agent(
            client, sessions, tenant_id=prior.tenant_id, agent_name=prior.name, actual_agent=actual
        )
    assert loaded.source is not None
    assert loaded.source.native == actual.model_dump(mode="json", exclude_unset=True)
    assert loaded.source.spec.system == payload["system"]
    assert loaded.source.spec.skills[0].version == "17"
    assert loaded.source.spec.mcp_servers[0].url == payload["mcp_servers"][0]["url"]
    assert (
        loaded.source.spec.extensions[0].value["tools"][0]["default_config"]["permission"]
        == "always_ask"
    )
    assert loaded.source.ref.account_scope_id == "", "unknown native workspace is not invented"
    for output in (repr(loaded), loaded.model_dump_json()):
        assert (
            "private source guidance" not in output
            and "https://mcp.example.test/source" not in output
        )
    assert len(transport.requests) == 1 and not transport.violations


async def test_source_projection_missing_unused_sdk_date_keeps_existing_preview(owned):
    from daimon.core.channel_backend import load_backend_agent
    from daimon.testing.ma_models import ma_agent
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    _, prior, _, sessions = owned
    payload = ma_agent(
        id="agent-partial", name=prior.name, tenant_id=str(prior.tenant_id)
    ).model_dump(mode="json")
    payload["created_at"] = None
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply("GET", "/v1/agents/agent-partial", httpx.Response(200, json=payload))
    )
    async with transport.client() as client:
        actual = await client.beta.agents.retrieve("agent-partial")
        loaded = await load_backend_agent(
            client, sessions, tenant_id=prior.tenant_id, agent_name=prior.name, actual_agent=actual
        )
    assert loaded.configuration_digest and loaded.model == actual.model.id
    assert loaded.source is None, "partial preview cannot stand for a complete execution source"
    assert len(transport.requests) == 1 and not transport.violations

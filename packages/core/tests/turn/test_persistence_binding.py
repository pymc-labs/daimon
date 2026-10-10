"""Prepared host admission selects the persisted private or N2 shared slot."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.shared_threads import (
    SharedThread,
    record_shared_binding,
    shared_owner,
    shared_slot,
)
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.turn.admission import Admission
from daimon.core.turn.binding import prepared_persistence
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.persistence import TurnPersistence
from daimon.testing.factories import make_account, make_tenant, make_thread_session
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ResourceRef, ThreadRef
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import ProviderBinding
from mux.errors import ScopeViolation
from mux.state.lease import Slot
from mux.state.memory import MemoryStateStore
from mux.state.store import binding_slot
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def deps(factory: async_sessionmaker[AsyncSession]) -> TurnDeps:
    return TurnDeps(
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal(1),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        turn_path="mux",
    )


def admission(account_id: uuid.UUID, tenant_id: uuid.UUID) -> Admission:
    return Admission(
        account_id=account_id,
        agent=ma_agent(id="agent", tenant_id=tenant_id),
        environment=ma_environment(id="environment", tenant_id=tenant_id),
        config=ResolvedConfig(agent_name="daimon", environment_name="default"),
        origin_channel_id="channel",
    )


async def test_private_prepared_binding_reuses_ids_and_advances_only_for_replacement(
    db_engine: AsyncEngine,
    db_clean: None,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        mapped = await make_thread_session(
            db,
            tenant=tenant,
            account=account,
            platform="slack",
            thread_id="thread",
            channel_id="channel",
            ma_session_id="first",
        )
    dependencies = deps(factory)
    admitted = admission(account.id, tenant.id)
    try:
        first = await prepared_persistence(
            dependencies,
            admitted,
            tenant_id=tenant.id,
            platform="slack",
            thread_id="thread",
            session_id="first",
            mapping_id=mapped.id,
            operation_key="message",
        )
        assert first.binding.id == str(mapped.id)
        assert first.binding.native_refs == {"session": "first", "agent": "agent"}
        assert binding_slot(first.binding).account_id == str(account.id)
        assert first.operation_key == "turn:first:message"
        again = await prepared_persistence(
            dependencies,
            admitted,
            tenant_id=tenant.id,
            platform="slack",
            thread_id="thread",
            session_id="first",
            mapping_id=mapped.id,
            operation_key="next",
        )
        assert again.binding == first.binding
        async with factory() as db, db.begin():
            replacement = await make_thread_session(
                db,
                tenant=tenant,
                account=account,
                platform="slack",
                thread_id="thread",
                channel_id="channel",
                ma_session_id="second",
            )
        second = await prepared_persistence(
            dependencies,
            admitted,
            tenant_id=tenant.id,
            platform="slack",
            thread_id="thread",
            session_id="second",
            mapping_id=replacement.id,
            operation_key="message",
        )
        assert second.binding.id == first.binding.id and second.binding.generation == 2
        assert second.binding.native_refs["session"] == "second"
        assert second.operation_key == "turn:second:message"
        with pytest.raises(ScopeViolation):
            await prepared_persistence(
                dependencies,
                admitted,
                tenant_id=tenant.id,
                platform="slack",
                thread_id="thread",
                session_id="first",
                mapping_id=mapped.id,
                operation_key="late-old-turn",
            )
        assert (
            await PostgresStateStore(factory).get_binding(binding_slot(second.binding))
            == second.binding
        )

    finally:
        await dependencies.anthropic.close()


async def test_shared_prepared_turn_uses_n2_persisted_binding_and_not_the_writer_slot(
    db_engine: AsyncEngine,
    db_clean: None,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = await make_tenant(db)
        alice = await make_account(db, tenant=tenant)
        bob = await make_account(db, tenant=tenant)
        slot = shared_slot(tenant.id, "slack", "channel", "thread")
        owner = shared_owner(slot)
        resolved = resolve_default(BackendConfig(thread_mode="shared"))
        revision = ConfigRevision(
            channel=slot.thread.channel,
            local=1,
            **resolved.model_dump(),
            digest=resolved.content_digest(),
        )
        shared = SharedThread(slot=slot, owner=owner, revision=revision, binding=None)
        expected = await record_shared_binding(db, shared, ma_session_id="shared-session")
        mapped = await create_thread_session(
            db,
            tenant_id=tenant.id,
            platform="slack",
            thread_id="thread",
            account_id=owner,
            ma_session_id="shared-session",
            channel_id="channel",
        )
    dependencies = deps(factory)
    try:
        contexts: list[TurnPersistence] = []
        for writer in (alice, bob):
            admitted = replace(
                admission(writer.id, tenant.id), shared_owner=owner, backend_revision=revision
            )
            context = await prepared_persistence(
                dependencies,
                admitted,
                tenant_id=tenant.id,
                platform="slack",
                thread_id="thread",
                session_id="shared-session",
                mapping_id=mapped.id,
                operation_key="message-" + str(writer.id),
            )
            assert context.binding == expected
            assert binding_slot(context.binding) == slot and slot.account_id is None
            assert context.scope.account_id == str(writer.id)
            ref = ResourceRef(
                id="shared-session",
                kind="session",
                provider="anthropic",
                account_scope_id="provider",
                tenant_id=str(tenant.id),
                account_id=str(writer.id),
            )

            async def pump(context: TurnPersistence = context, ref: ResourceRef = ref) -> None:
                context.check_session(context.scope, ref)

                async def deliver(key: str) -> SendReceipt:
                    return SendReceipt(operation_id=key, status="processed", input_ids=("input",))

                await context.mutate(
                    ref, "send", {"text": "question"}, deliver, SendReceipt, lambda _: "processed"
                )
                await context.record(
                    ref,
                    Event(
                        id=context.scope.account_id,
                        session_id=ref.id,
                        sequence=0,
                        type="native.writer",
                        observed_at=datetime.now(UTC),
                        authority="record",
                        payload={},
                        native=NativeProvenance(
                            provider="anthropic",
                            api_revision="test",
                            event_id=context.scope.account_id,
                        ),
                    ),
                )

            await context.run(pump)
            contexts.append(context)
        assert contexts[0].binding.id == contexts[1].binding.id == str(owner)
        store = PostgresStateStore(factory)
        journal = await store.read_events("shared-session")
        assert [(event.sequence, event.id) for event in journal] == [
            (0, str(alice.id)),
            (1, str(bob.id)),
        ]
        for context in contexts:
            record = await store.get_operation(context.scope, context.operation_key + ":send:0")
            assert record is not None and record.slot == slot
            assert record.account_id == context.scope.account_id
        for writer in (alice, bob):
            assert (
                await store.get_binding(Slot(thread=slot.thread, account_id=str(writer.id))) is None
            )
    finally:
        await dependencies.anthropic.close()


@pytest.mark.parametrize("mismatch", ("tenant", "account", "session", "thread", "platform"))
async def test_foreign_prepared_mapping_fails_before_binding_publication(
    db_engine: AsyncEngine,
    db_clean: None,
    mismatch: str,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        mapped = await make_thread_session(
            db,
            tenant=tenant,
            account=account,
            platform="slack",
            thread_id="thread",
            channel_id="channel",
            ma_session_id="session",
        )
    dependencies = deps(factory)
    admitted = admission(uuid.UUID(int=999) if mismatch == "account" else account.id, tenant.id)
    try:
        with pytest.raises(ScopeViolation):
            await prepared_persistence(
                dependencies,
                admitted,
                tenant_id=uuid.UUID(int=998) if mismatch == "tenant" else tenant.id,
                platform="foreign" if mismatch == "platform" else "slack",
                thread_id="foreign" if mismatch == "thread" else "thread",
                session_id="foreign" if mismatch == "session" else "session",
                mapping_id=mapped.id,
                operation_key="message",
            )
        store = PostgresStateStore(factory)
        assert (
            await store.get_binding(
                Slot(
                    thread=shared_slot(tenant.id, "slack", "channel", "thread").thread,
                    account_id=str(account.id),
                )
            )
            is None
        )
    finally:
        await dependencies.anthropic.close()


async def test_missing_shared_binding_is_not_claimed_by_the_turn(
    db_engine: AsyncEngine,
    db_clean: None,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    dependencies = deps(factory)
    slot = shared_slot(tenant.id, "slack", "channel", "thread")
    resolved = resolve_default(BackendConfig(thread_mode="shared"))
    revision = ConfigRevision(
        channel=slot.thread.channel,
        local=1,
        **resolved.model_dump(),
        digest=resolved.content_digest(),
    )
    admitted = replace(
        admission(account.id, tenant.id), shared_owner=shared_owner(slot), backend_revision=revision
    )
    try:
        with pytest.raises(ScopeViolation, match="persisted binding"):
            await prepared_persistence(
                dependencies,
                admitted,
                tenant_id=tenant.id,
                platform="slack",
                thread_id="thread",
                session_id="unbound",
                mapping_id=None,
                operation_key="message",
            )
        assert await PostgresStateStore(factory).get_binding(slot) is None
    finally:
        await dependencies.anthropic.close()


@pytest.mark.parametrize("path", ("legacy", "mux"))
async def test_prepared_driver_wires_state_only_on_mux_and_reuses_explicit_root_key(
    db_engine: AsyncEngine,
    db_clean: None,
    path: str,
) -> None:
    import asyncio
    from typing import Literal, cast

    from daimon.core.turn.prepare import PreparedTurn
    from daimon.core.turn.run import run_prepared_turn
    from daimon.testing.ma import send_events_response
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
    from daimon.testing.turn_fakes import RecordingLifecycle

    from .conftest import make_agent_message, make_status_idle

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        mapped = await make_thread_session(
            db,
            tenant=tenant,
            account=account,
            platform="slack",
            thread_id="thread",
            channel_id="channel",
            ma_session_id="session",
        )
    transport = ScriptedTransport()
    raw = [
        make_agent_message(event_id="message", text="answer").model_dump(mode="json"),
        make_status_idle(event_id="ended").model_dump(mode="json"),
    ]
    transport.queue(
        ScriptedReply.stream("/v1/sessions/session/events/stream", raw),
        ScriptedReply("POST", "/v1/sessions/session/events", send_events_response()),
    )
    async with transport.client() as client:
        dependencies = replace(
            deps(factory), anthropic=client, turn_path=cast(Literal["legacy", "mux"], path)
        )
        admitted = admission(account.id, tenant.id)

        async def record(event: object) -> None:
            pass

        prepared = PreparedTurn(
            admission=admitted,
            ma_session_id="session",
            mapping_id=mapped.id,
            watermark=None,
            reused=True,
            session_account_id=account.id,
            _record=record,
        )
        lifecycle = RecordingLifecycle()

        async def reseed() -> str:
            raise AssertionError("healthy session must not recover")

        result = await run_prepared_turn(
            dependencies,
            prepared,
            tenant_id=tenant.id,
            platform="slack",
            thread_id="thread",
            external_user_id="writer",
            user_message="question",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda _: lifecycle,
            operation_key="platform-message",
        )
        assert [block.text for block in result.state.content if block.kind == "text"] == ["answer"]
        assert result.state.error is None
        store = PostgresStateStore(factory)
        slot = Slot(
            thread=shared_slot(tenant.id, "slack", "channel", "thread").thread,
            account_id=str(account.id),
        )
        binding = await store.get_binding(slot)
        if path == "legacy":
            assert binding is None
            assert not await store.read_events("session")
        else:
            assert binding is not None and binding.native_refs["session"] == "session"
            assert len(await store.read_events("session")) == 2
            operation = await store.get_operation(
                (
                    await prepared_persistence(
                        dependencies,
                        admitted,
                        tenant_id=tenant.id,
                        platform="slack",
                        thread_id="thread",
                        session_id="session",
                        mapping_id=mapped.id,
                        operation_key="platform-message",
                    )
                ).scope,
                "turn:session:platform-message:send:0",
            )
            assert operation is not None and operation.operation.status == "accepted"
    transport.assert_consumed()


async def test_unmapped_binding_never_consumes_a_host_identity_and_new_invocations_have_new_keys(
    monkeypatch: pytest.MonkeyPatch,
    db_engine: AsyncEngine,
) -> None:
    from daimon.core.turn.outcomes import TurnObservation
    from mux.state.memory import MemoryStateStore

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    dependencies = replace(deps(factory), state_store=MemoryStateStore())
    tenant_id = uuid.UUID(int=4406)
    account_id = uuid.UUID(int=4407)
    admitted = admission(account_id, tenant_id)
    observed = [
        TurnObservation(factory, tenant_id, "slack", id=uuid.UUID(int=4408)),
        TurnObservation(factory, tenant_id, "slack", id=uuid.UUID(int=4409)),
    ]

    def no_host_uuid() -> uuid.UUID:
        raise AssertionError("binding must not allocate a host control identity")

    monkeypatch.setattr(uuid, "uuid4", no_host_uuid)
    contexts: list[TurnPersistence] = []
    try:
        for observation in observed:
            with observation.activate():
                contexts.append(
                    await prepared_persistence(
                        dependencies,
                        admitted,
                        tenant_id=tenant_id,
                        platform="slack",
                        thread_id="private-thread",
                        session_id="private-session",
                        mapping_id=None,
                    )
                )
        assert contexts[0].binding == contexts[1].binding
        assert contexts[0].operation_key != contexts[1].operation_key
        assert contexts[0].operation_key == f"turn:private-session:{observed[0].id}"
        resumed = await prepared_persistence(
            dependencies,
            admitted,
            tenant_id=tenant_id,
            platform="slack",
            thread_id="private-thread",
            session_id="private-session",
            mapping_id=None,
            operation_key=str(observed[0].id),
        )
        assert resumed.operation_key == contexts[0].operation_key
    finally:
        await dependencies.anthropic.close()


@pytest.mark.parametrize("native_session", ["same-id", "old-foreign-id"])
async def test_anthropic_preparation_cannot_adopt_a_foreign_persisted_binding(
    db_engine: AsyncEngine, native_session: str
) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    store = MemoryStateStore()
    expected = ProviderBinding(
        id="provider-owned-binding",
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=str(tenant_id), platform="slack", channel_id="channel"),
            thread_id="thread",
        ),
        provider="openai",
        profile="openai.persistent_workspace",
        native_refs={"session": native_session},
        generation=1,
        config_revision=1,
        legacy_account_id=str(account_id),
    )
    await store.put_binding(expected, expected_generation=0)
    dependencies = replace(deps(async_sessionmaker(db_engine)), state_store=store)
    try:
        with pytest.raises(ScopeViolation, match="another provider profile"):
            await prepared_persistence(
                dependencies,
                admission(account_id, tenant_id),
                tenant_id=tenant_id,
                platform="slack",
                thread_id="thread",
                session_id="same-id",
                mapping_id=None,
            )
        assert await store.get_binding(binding_slot(expected)) == expected
    finally:
        await dependencies.anthropic.close()


@pytest.mark.parametrize(
    "change", [None, "missing", "profile", "session", "revision", "ref", "mapping"]
)
async def test_provider_preparation_consumes_only_its_preexisting_native_binding(
    db_engine: AsyncEngine, change: str | None
) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    store = MemoryStateStore()
    channel = ChannelRef(tenant_id=str(tenant_id), platform="slack", channel_id="channel")
    revision = ConfigRevision.create(
        channel,
        3,
        resolve_default(
            BackendConfig(
                backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna"
            )
        ),
    )
    expected = ProviderBinding(
        id="provider-preparation-owner",
        thread=ThreadRef(channel=channel, thread_id="thread"),
        provider="openai",
        profile=revision.profile,
        native_refs={"session": "native-session"},
        generation=2,
        config_revision=revision.local,
        legacy_account_id=str(account_id),
    )
    if change == "profile":
        expected = expected.model_copy(
            update={"provider": "anthropic", "profile": "anthropic.managed_agents"}
        )
    elif change == "session":
        expected = expected.model_copy(update={"native_refs": {"session": "old-session"}})
    elif change == "revision":
        expected = expected.model_copy(update={"config_revision": 2})
    if change != "missing":
        await store.put_binding(
            expected.model_copy(update={"generation": 1}), expected_generation=0
        )
        await store.put_binding(expected, expected_generation=1)
    dependencies = replace(deps(async_sessionmaker(db_engine)), state_store=store)
    admitted = replace(admission(account_id, tenant_id), backend_revision=revision)
    ref = ResourceRef(
        id="native-session",
        kind="session",
        provider="openai",
        account_scope_id="project",
        tenant_id=str(tenant_id),
        account_id=str(account_id),
    )
    sends: list[str] = []

    async def deliver(key: str) -> SendReceipt:
        sends.append(key)
        return SendReceipt(operation_id=key, status="processed", input_ids=("input",))

    try:
        if change is not None:
            with pytest.raises(ScopeViolation):
                await prepared_persistence(
                    dependencies,
                    admitted,
                    tenant_id=tenant_id,
                    platform="slack",
                    thread_id="thread",
                    session_id=ref.id,
                    mapping_id=uuid.uuid4() if change == "mapping" else None,
                    session_ref=None if change == "ref" else ref,
                    operation_key="known-invocation",
                )
            assert await store.get_binding(binding_slot(expected)) == (
                None if change == "missing" else expected
            )
            assert sends == []
            return
        for _ in range(2):
            persistence = await prepared_persistence(
                dependencies,
                admitted,
                tenant_id=tenant_id,
                platform="slack",
                thread_id="thread",
                session_id=ref.id,
                mapping_id=None,
                session_ref=ref,
                operation_key="known-invocation",
            )
            assert persistence.binding == expected

            async def pump(bound: TurnPersistence = persistence) -> SendReceipt:
                return await bound.mutate(
                    ref,
                    "send",
                    {"text": "question"},
                    deliver,
                    SendReceipt,
                    lambda receipt: "processed",
                )

            receipt = await persistence.run(pump)
            assert receipt.input_ids == ("input",)
        assert sends == ["turn:native-session:known-invocation:send:0"]
        assert await store.get_binding(binding_slot(expected)) == expected
    finally:
        await dependencies.anthropic.close()

"""C04 facts through the actual SDK, without invented lookup or redelivery."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec, Session, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.transport import NO_TOOLS_TOOL, SDKTransport
from mux.errors import OperationConflict, ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import CrashPoint, MemoryStateStore, SimulatedCrash
from mux.testing.drivers.gemini.conformance_hooks import recover_send
from mux.testing.drivers.gemini.fake import MemoryStorage
from mux.testing.isolated_sdk import build_gemini_client

pytestmark = pytest.mark.usefixtures("gemini_recorded_clock")

SCOPE = Scope(tenant_id="n12", account_id="offline", principal_id="human", authorization_id="auth")
INPUT = (UserMessage(content=(TextPart(text="same text is not native identity"),)),)
STAMP = datetime(2026, 10, 10, tzinfo=UTC).isoformat()


class NativeAcceptance:
    def __init__(self, *, lose_reply: bool = False) -> None:
        self.lose_reply = lose_reply
        self.requests: list[httpx.Request] = []
        self.accepted: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            assert self.accepted and not self.lose_reply
            assert request.url.path.endswith("/interactions/native-input")
        else:
            assert request.method == "POST"
            self.accepted.append(request.content)
        if self.lose_reply:
            raise httpx.ReadError("scripted loss after native acceptance", request=request)
        return httpx.Response(
            200,
            json={
                "id": "native-input",
                "status": "completed",
                "created": STAMP,
                "updated": STAMP,
                "environment_id": "workspace",
                "steps": [],
            },
        )


async def arrange(ma: GeminiManagedAgents) -> Session:
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(name="proof", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    env = await ma.environments.create(SCOPE, EnvironmentSpec(name="proof"), key="env")
    return await ma.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=env.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={
                        "binding_id": "binding",
                        "thread": {
                            "channel": {
                                "tenant_id": "n12",
                                "platform": "test",
                                "channel_id": "channel",
                            },
                            "thread_id": "thread",
                        },
                    },
                )
            },
        ),
        key="session",
    )


@asynccontextmanager
async def sdk(native: NativeAcceptance) -> AsyncIterator[SDKTransport]:
    with build_gemini_client(
        transport=httpx.MockTransport(native), api_key="offline-fixture"
    ) as client:
        try:
            yield SDKTransport(client)
        finally:
            await client.aio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "point", "status", "post_count"),
    [
        ("claim_send", "before_commit", "pending", 0),
        ("claim_send", "after_commit", "sent", 0),
        ("advance_operation", "before_commit", "sent", 1),
        ("advance_operation", "after_commit", "accepted", 1),
    ],
)
async def test_real_sdk_claim_and_ack_crashes_never_blindly_resend(
    method: str,
    point: CrashPoint,
    status: str,
    post_count: int,
) -> None:
    native, storage, store = NativeAcceptance(), MemoryStorage(), MemoryStateStore()
    async with sdk(native) as transport:
        store = store.restart(crash={method: point})
        ma = GeminiManagedAgents(
            transport, storage=storage, state_store=store, account_scope_id="p"
        )
        session = await arrange(ma)
        with pytest.raises(SimulatedCrash):
            await ma.events.send(SCOPE, session.ref, INPUT, key="operation")
        store = store.restart()
        ma = GeminiManagedAgents(
            transport, storage=storage, state_store=store, account_scope_id="p"
        )
        record = await store.get_operation(SCOPE, "operation")
        assert record is not None and record.operation.status == status
        assert len(native.accepted) == post_count
        if status == "pending":
            with pytest.raises(UnsupportedCapability):
                await recover_send(ma, store, SCOPE, session.ref, INPUT, key="operation")
            assert not native.requests
            receipt = await ma.events.send(SCOPE, session.ref, INPUT, key="operation")
            assert receipt.status == "queued" and len(native.accepted) == 1
        else:
            evidence = await recover_send(ma, store, SCOPE, session.ref, INPUT, key="operation")
            assert evidence.receipt.operation_id == record.operation.id
            if status == "accepted":
                assert evidence.basis == "durable_acknowledgement"
                assert evidence.receipt.input_ids == ("native-input",)
                assert evidence.receipt.status == "queued"  # acceptance is not processed completion
            else:
                assert evidence.basis == "native_lookup_unavailable"
                assert evidence.receipt.status == "outcome_unknown"
                assert evidence.receipt.input_ids == () and evidence.receipt.turn_id is None
            assert await store.get_operation(SCOPE, "operation") == record
            assert len(native.requests) == post_count
        if native.accepted:
            body = json.loads(native.accepted[0])
            content = INPUT[0].content[0]
            assert isinstance(content, TextPart)
            assert body["input"] == [{"type": "text", "text": content.text}]
            assert body["tools"] == [NO_TOOLS_TOOL]
            assert body["agent_config"]["model"] == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_lost_reply_keeps_unknown_and_blocked_after_restart() -> None:
    native, storage, store = NativeAcceptance(lose_reply=True), MemoryStorage(), MemoryStateStore()
    async with sdk(native) as transport:
        ma = GeminiManagedAgents(
            transport, storage=storage, state_store=store, account_scope_id="p"
        )
        session = await arrange(ma)
        receipt = await ma.events.send(SCOPE, session.ref, INPUT, key="operation")
        assert receipt.status == "outcome_unknown" and len(native.accepted) == 1
        store = store.restart()
        ma = GeminiManagedAgents(
            transport, storage=storage, state_store=store, account_scope_id="p"
        )
        recovered = await recover_send(ma, store, SCOPE, session.ref, INPUT, key="operation")
        assert recovered.receipt == receipt
        assert await ma.events.send(SCOPE, session.ref, INPUT, key="operation") == receipt
        with pytest.raises(UnsupportedCapability):
            await ma.events.send(SCOPE, session.ref, INPUT, key="other-key")
        assert len(native.accepted) == len(native.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["input", "principal", "tenant", "account", "key"])
async def test_recovery_refuses_changed_authority_or_immutable_input(change: str) -> None:
    native, storage, store = NativeAcceptance(lose_reply=True), MemoryStorage(), MemoryStateStore()
    async with sdk(native) as transport:
        ma = GeminiManagedAgents(
            transport, storage=storage, state_store=store, account_scope_id="p"
        )
        session = await arrange(ma)
        await ma.events.send(SCOPE, session.ref, INPUT, key="operation")
        scope, inputs, key = SCOPE, INPUT, "operation"
        if change == "input":
            inputs = (UserMessage(content=(TextPart(text="changed"),)),)
        elif change == "key":
            key = "absent"
        else:
            scope = SCOPE.model_copy(update={f"{change}_id": "foreign"})
        with pytest.raises((ScopeViolation, OperationConflict)):
            await recover_send(ma, store, scope, session.ref, inputs, key=key)
        assert len(native.requests) == 1


@pytest.mark.asyncio
async def test_missing_native_ack_identity_is_not_durable_acknowledgement() -> None:
    native, storage, store = NativeAcceptance(), MemoryStorage(), MemoryStateStore()
    async with sdk(native) as transport:
        ma = GeminiManagedAgents(
            transport, storage=storage, state_store=store, account_scope_id="p"
        )
        session = await arrange(ma)
        await ma.events.send(SCOPE, session.ref, INPUT, key="operation")
        # Retain a real accepted operation but corrupt its acknowledgement independently.
        await store.advance_operation(
            SCOPE, "operation", "outcome_unknown", now=datetime.now(UTC), fence=None, result={}
        )
        result = await recover_send(ma, store, SCOPE, session.ref, INPUT, key="operation")
        assert result.receipt.status == "outcome_unknown" and result.receipt.input_ids == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_field", ["input_ids", "turn_id", "operation_id", "status", "resource"]
)
async def test_invalid_durable_acknowledgement_cannot_report_success(bad_field: str) -> None:
    native, storage, store = NativeAcceptance(), MemoryStorage(), MemoryStateStore()
    async with sdk(native) as transport:
        ma = GeminiManagedAgents(
            transport, storage=storage, state_store=store, account_scope_id="p"
        )
        session = await arrange(ma)
        receipt = await ma.events.send(SCOPE, session.ref, INPUT, key="operation")
        ack = receipt.model_dump(mode="json")
        resource = session.ref
        if bad_field == "resource":
            resource = resource.model_copy(update={"id": "other-session"})
        elif bad_field == "input_ids":
            ack[bad_field] = []
        elif bad_field == "turn_id":
            ack[bad_field] = None
        elif bad_field == "operation_id":
            ack[bad_field] = "other-operation"
        else:
            ack[bad_field] = "processed"
        await store.advance_operation(
            SCOPE, "operation", "outcome_unknown", now=datetime.now(UTC), fence=None
        )
        await store.advance_operation(
            SCOPE,
            "operation",
            "accepted",
            now=datetime.now(UTC),
            fence=None,
            resource=resource,
            result=ack,
        )
        with pytest.raises(ProviderError, match="invalid_acknowledgement"):
            await recover_send(ma, store, SCOPE, session.ref, INPUT, key="operation")
        assert len(native.accepted) == 1
        assert len(native.requests) == 2  # One original POST and the exact owned session GET.

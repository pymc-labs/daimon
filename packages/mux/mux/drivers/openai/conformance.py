"""Opt-in offline native HTTP/SSE adapter for the actual OpenAI driver.

No provider discovery, credentials or sockets. Unsupported matrix entries are
explicit pending dependencies; the adapter never supplies pass verdicts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from types import MappingProxyType, TracebackType

import httpx
from openai import AsyncOpenAI

from mux.conformance.runner import (
    Adapter,
    PendingKind,
    PendingReason,
    Registry,
    Scenario,
    SendEvidence,
)
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import SessionSpec, SkillUpload
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import Object, SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.state.memory import CrashPoint
from mux.state.store import StateStore

SCRIPTED_FIXTURES = frozenset({"C05", "C06", "C09", "C10", "C15", "C16"})

PENDING: Mapping[str, PendingReason] = MappingProxyType(
    {
        "C01": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host attribution, batching and workspace binding adapter not wired",
        ),
        "C02": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "native hosted expiry classification and host session preparation unverified",
        ),
        "C03": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host intent, atomic send claiming and restart receipt recovery not injected",
        ),
        "C04": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host lease/fencing and transactional journal crash-recovery adapter are not injected",
        ),
        "C07": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "current native usage snapshots need a shared revision/outbox StateStore adapter",
        ),
        "C08": PendingReason(
            PendingKind.CAPABILITY_UNAVAILABLE,
            "conditional required-mount replacement refused; host preparation adapter absent",
        ),
        "C11": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "deployed repository/MCP bindings and separate display titles are unavailable",
        ),
        "C12": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "billing identity and overlapping-grain pricing need the host certification hook",
        ),
        "C13": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host new-slot binding adoption and restart persistence are not injected",
        ),
        "C14": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "existing/new-thread provider registry selection needs the Daimon host adapter",
        ),
        "C17": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host wake-generation fencing is not exercised by provider event normalization",
        ),
        "C18": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host termination/outcome-row mapping is not supplied; mux cannot import daimon",
        ),
    }
)


SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="human", authorization_id="grant"
)
REF = ResourceRef(
    id="session",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id="tenant",
    account_id="account",
)


def native_turn(status: str = "in_progress", *, child: bool = False) -> Object:
    return {
        "id": "child" if child else "root",
        "session_id": "session",
        "agent_id": "agent",
        "subagent_id": "subagent" if child else None,
        "created_at": 0,
        "status": status,
        "usage": None,
    }


def native_message() -> Object:
    return {
        "id": "item",
        "turn_id": "root",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "done"}],
    }


def native_result() -> Object:
    return {
        "id": "result",
        "turn_id": "root",
        "type": "function_call_output",
        "call_id": "call",
        "status": "completed",
        "output": "result",
    }


def native_event(kind: str, id_: str, value: Object) -> Object:
    return {"event_id": id_, "session_id": "session", "type": "agent.session." + kind, **value}


class _SSE(httpx.AsyncByteStream):
    def __init__(self, events: tuple[Object, ...], *, hold: bool) -> None:
        self.events, self.hold = events, hold
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self.events:
            yield (
                "event: " + str(event["type"]) + "\ndata: " + json.dumps(event) + "\n\n"
            ).encode()
        if self.hold:
            await self.closed.wait()

    async def aclose(self) -> None:
        self.closed.set()


class OpenAIScriptedTransport:
    """Seed documented native replies and log wire requests independently.

    Only request/snapshot state changes here. Projections, normalized events and
    receipts are exclusively produced by the driver.
    """

    def __init__(self) -> None:
        self.fixture = ""
        self.faults: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.streams: list[_SSE] = []
        self.recovering = False
        self.driver: OpenAIDriver | None = None
        self._sdk: AsyncOpenAI | None = None

    def attach(self, driver: OpenAIDriver, sdk: AsyncOpenAI) -> None:
        self.driver, self._sdk = driver, sdk

    async def aclose(self) -> None:
        for stream in self.streams:
            await stream.aclose()
        if self._sdk is not None:
            await self._sdk.close()

    @property
    def closed(self) -> bool:
        return self._sdk is None or self._sdk.is_closed()

    def _status(self) -> str:
        if self.fixture == "C05" and self.recovering:
            return "completed"
        if "observed_stop" in self.faults:
            return "cancelled"
        return "in_progress" if self.fixture in ("C05", "C06") else "completed"

    def _session(self) -> Object:
        return {
            "id": "session",
            "agent": {"id": "agent", "model": "fixture"},
            "created_at": 0,
            "environment": {"type": "openai_hosted", "id": "environment"},
            "status": "in_progress" if self._status() == "in_progress" else "idle",
            "required_actions": [],
            "metadata": {"mux_tenant": "tenant"},
            "vault_ids": ["shared-vault"] if self.fixture == "C09" else [],
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1")
        if request.headers.get("openai-beta") != "agents=v1":
            return httpx.Response(
                400, json={"error": {"code": "invalid_beta", "message": "fixture"}}
            )
        if self.fixture == "C09":
            if path.startswith("/agents/sessions/session/artifacts"):
                if path.endswith("/content"):
                    size = 17 if "download_interrupted" in self.faults else 256
                    return httpx.Response(200, content=bytes(range(size)))
                second = bool(request.url.params.get("after"))
                listing = path.endswith("/artifacts")
                identity = ("second" if second else "first") if listing else path.rsplit("/", 1)[-1]
                artifact: Object = {
                    "id": identity,
                    "session_id": "session",
                    "turn_id": "root",
                    "environment_id": "environment",
                    "size_bytes": 256,
                    "created_at": 0,
                    "path": "/workspace/outputs/" + identity + ".bin",
                }
                if listing:
                    return httpx.Response(
                        200,
                        json={
                            "data": [artifact],
                            "has_more": not second,
                            "last_id": identity,
                        },
                    )
                return httpx.Response(200, json=artifact)
            if request.method == "DELETE" and path in (
                "/agents/sessions/session",
                "/vaults/shared-vault",
            ):
                return httpx.Response(200, json={})
        if path == "/agents/sessions/session/events" and request.method == "GET":
            if (
                request.url.params.get("stream") != "true"
                or request.headers.get("Accept") != "text/event-stream"
            ):
                raise RuntimeError("fixture requires documented SSE negotiation")
            if self.fixture == "C05":
                self.recovering = bool(self.streams)
                values = [
                    native_event(
                        "turn.completed",
                        "child-end",
                        {"turn": native_turn("completed", child=True)},
                    ),
                    native_event(
                        "turn.output_text.delta",
                        "preview",
                        {
                            "turn_id": "root",
                            "item_id": "item",
                            "content_index": 0,
                            "delta": "preview",
                        },
                    ),
                    native_event(
                        "turn.item.done",
                        "message-final",
                        {"turn_id": "root", "item": native_message()},
                    ),
                ]
                if self.recovering:
                    values.append(
                        native_event(
                            "turn.completed", "root-end", {"turn": native_turn("completed")}
                        )
                    )
            else:
                values = []
            source = _SSE(tuple(values), hold=self.recovering)
            self.streams.append(source)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
        if path == "/agents/sessions/session" and request.method == "GET":
            return httpx.Response(200, json=self._session())
        if path == "/agents/sessions/session/turns" and request.method == "GET":
            values = [native_turn(self._status()), native_turn("completed", child=True)]
            return httpx.Response(200, json={"data": values, "has_more": False, "last_id": "child"})
        if path == "/agents/sessions/session/turns/root" and request.method == "GET":
            return httpx.Response(200, json=native_turn(self._status()))
        if path == "/agents/sessions/session/items" and request.method == "GET":
            if self.fixture == "C05":
                after = request.url.params.get("after")
                values = [native_message(), native_result()] if after else [native_message()]
                return httpx.Response(
                    200,
                    json={
                        "data": values,
                        "has_more": not after,
                        "last_id": "result" if after else "item",
                    },
                )
            return httpx.Response(200, json={"data": [], "has_more": False, "last_id": None})
        if path == "/agents/sessions/session/events" and request.method == "POST":
            body = json.loads(request.content)
            if (
                body != {"events": [{"type": "agent.session.input.cancel"}]}
                or request.headers.get("Idempotency-Key") != "cancel"
            ):
                return httpx.Response(400, json={"error": {"message": "fixture unexpected input"}})
            return httpx.Response(202)
        return httpx.Response(404, json={"error": {"message": "fixture path not seeded"}})

    async def arrange(self, fixture_id: str) -> Scenario:
        if fixture_id not in SCRIPTED_FIXTURES:
            raise ValueError("fixture is not scripted by this core adapter")
        self.fixture = fixture_id
        if self.driver is None:
            raise RuntimeError("driver not attached")
        session = await self.driver.sessions.retrieve(SCOPE, REF)
        return Scenario(
            scope=SCOPE,
            foreign_scope=SCOPE.model_copy(update={"tenant_id": "foreign"}),
            session=session,
            shared_resources=(REF.model_copy(update={"id": "shared-vault", "kind": "vault"}),)
            if fixture_id == "C09"
            else (),
            desired=SessionSpec(
                agent=REF.model_copy(update={"id": "agent", "kind": "agent"}),
                agent_revision=Revision(local=0),
                config_revision=0,
            ),
        )

    def fault(self, name: str) -> None:
        if name not in (
            "observed_stop",
            "admission_unknown",
            "admission_unsupported",
            "download_interrupted",
        ):
            raise ValueError("fault is not scripted by this core adapter")
        # Admission uses a fixed, evidence-backed profile. These two fixture
        # labels cannot alter native capabilities or driver projected state.
        # Separate tests exercise its actual unknown and unsupported entries.
        self.faults.add(name)

    @property
    def mutation_count(self) -> int:
        return sum(request.method != "GET" for request in self.requests)

    @property
    def deleted_resources(self) -> tuple[ResourceRef, ...]:
        deleted: list[ResourceRef] = []
        for request in self.requests:
            if request.method == "DELETE":
                path = request.url.path.removeprefix("/v1")
                if path == "/agents/sessions/session":
                    deleted.append(REF)
                elif path == "/vaults/shared-vault":
                    deleted.append(REF.model_copy(update={"id": "shared-vault", "kind": "vault"}))
        return tuple(deleted)

    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]:
        return ()

    @property
    def upstream_sends(self) -> tuple[SendEvidence, ...]:
        return ()

    @property
    def reconciled_sends(self) -> tuple[SendEvidence, ...]:
        return ()

    def restart_store(
        self, store: StateStore, *, crash: Mapping[str, CrashPoint] | None = None
    ) -> StateStore:
        raise RuntimeError("host store bridge not implemented")


def factory() -> Adapter:
    script = OpenAIScriptedTransport()
    sdk = AsyncOpenAI(
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(script.handle)),
    )
    driver = OpenAIDriver(
        SDKTransport(sdk),
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda scope, kind, id_: scope == SCOPE,
    )
    script.attach(driver, sdk)
    return Adapter(driver=driver, store=None, transport=script, pending=PENDING)


class OpenAIOfflineFactory:
    """Fresh probes, with explicit SDK/stream cleanup for an offline run."""

    def __init__(self) -> None:
        self._scripts: list[OpenAIScriptedTransport] = []

    def __call__(self) -> Adapter:
        adapter = factory()
        if not isinstance(adapter.transport, OpenAIScriptedTransport):
            raise TypeError("invalid offline transport")
        self._scripts.append(adapter.transport)
        return adapter

    async def __aenter__(self) -> OpenAIOfflineFactory:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for script in self._scripts:
            await script.aclose()
        self._scripts.clear()


def register(registry: Registry) -> OpenAIOfflineFactory:
    adapters = OpenAIOfflineFactory()
    registry.register("openai.offline", adapters)
    return adapters

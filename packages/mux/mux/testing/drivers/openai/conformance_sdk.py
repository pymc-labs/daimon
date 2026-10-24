"""Explicit offline resource SDK boundaries; never a live certificate or factory."""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
from openai import AsyncOpenAI

from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai._common import Context
from mux.drivers.openai.transport import Object, SDKTransport, object_json
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions, OpenAIUsage
from mux.state.memory import CrashPoint, MemoryStateStore
from mux.state.store import StateStore
from mux.testing.conformance.runner import Adapter, PendingKind, PendingReason, Scenario
from mux.testing.drivers.openai.conformance import REF, SCOPE, OpenAIScriptedTransport, native_turn
from mux.testing.drivers.openai.offline_sdk import SDKFactory, build_offline_sdk
from mux.testing.drivers.openai.offline_sdk import (
    refuse_inherited_settings as _refuse_inherited_settings,
)


class OpenAISDKScript(OpenAIScriptedTransport):
    """Fixed native snapshots/faults, with independent HTTP mutation evidence.

    The snapshot probe controls four successive GET observations, not a fabricated batch of
    duplicate turns or usage values supplied directly to the ledger. Its
    revision allocator is retained on restart. No price or settlement follows.
    """

    def __init__(self) -> None:
        super().__init__()
        self.store = MemoryStateStore()
        self.binding: ProviderBinding | None = None
        self.usage_reads = 0
        self.revisions = MemoryUsageRevisions()
        self.journal = MemoryRecoveryJournal()

    def _session(self) -> Object:
        native = super()._session()
        native["agent"] = {"id": "agent", "model": "gpt-6-luna"}
        return native

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        if (
            self.fixture == "C08"
            and request.method == "POST"
            and path
            in (
                "/agents",
                "/agents/environments/templates",
            )
        ):
            self.requests.append(request)
            if request.headers.get("OpenAI-Beta") != "agents=v1":
                raise ValueError("fixture requires the documented beta header")
            body = object_json(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "changed-agent" if path == "/agents" else "changed-template",
                    "created_at": 0,
                    **body,
                },
            )
        if self.fixture == "C07" and path == "/agents/sessions/session/turns":
            self.requests.append(request)
            if request.method != "GET" or request.headers.get("OpenAI-Beta") != "agents=v1":
                raise ValueError("usage fixture requires the documented GET and beta header")
            count = (None, 100, 120, 110)[min(self.usage_reads, 3)]
            self.usage_reads += 1
            turn = native_turn("completed")
            turn["usage"] = None if count is None else {"input_tokens": 0, "output_tokens": count}
            return httpx.Response(200, json={"data": [turn], "has_more": False, "last_id": "root"})
        return await super().handle(request)

    async def arrange(self, fixture_id: str) -> Scenario:
        if fixture_id not in ("C07", "C08"):
            return await super().arrange(fixture_id)
        # Reuse the actual saved native session decoder, not a projected Session.
        scenario = await super().arrange("C10")
        self.fixture = fixture_id
        if fixture_id == "C07":
            if self.driver is None:
                raise ValueError("driver is not attached")
            self.binding = ProviderBinding(
                id="sdk-usage-binding",
                thread=ThreadRef(
                    channel=ChannelRef(
                        tenant_id="tenant", platform="offline", channel_id="channel"
                    ),
                    thread_id="thread",
                ),
                provider="openai",
                profile=self.driver.capabilities().profile_id,
                native_refs={"session": "session", "agent": "agent", "environment": "environment"},
                generation=1,
                config_revision=1,
                legacy_account_id=SCOPE.account_id,
            )
            session = await self.driver.sessions.retrieve(SCOPE, REF)
            await self.store.put_binding(session.binding, expected_generation=0)
            scenario = Scenario(
                scope=SCOPE,
                foreign_scope=scenario.foreign_scope,
                session=session,
                desired=scenario.desired,
            )
        return scenario

    def restart_store(
        self, store: StateStore, *, crash: Mapping[str, CrashPoint] | None = None
    ) -> StateStore:
        if self.fixture != "C07" or not isinstance(store, MemoryStateStore):
            raise ValueError("this component restarts only C07 memory accounting")
        self.store = store.restart(crash=crash)
        self.reconstruct_driver()
        return self.store

    def reconstruct_driver(self) -> OpenAIDriver:
        """Reuse the owned SDK and native storage; replace only driver-local objects."""
        if self._sdk is None:
            raise ValueError("SDK is not attached")
        return _attach(self, self._sdk)


class _UsageSequence(OpenAIUsage):
    """Fixture-only sampling of successive native usage revisions."""

    def __init__(self, context: Context, script: OpenAISDKScript) -> None:
        super().__init__(context, script.revisions)
        self._script = script

    async def reconcile(self, scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        if self._script.fixture != "C07":
            return await super().reconcile(scope, session)
        observations: list[UsageObservation] = []
        for _ in range(4):
            observations.extend(await super().reconcile(scope, session))
        return tuple(observations)


def _attach(script: OpenAISDKScript, sdk: AsyncOpenAI) -> OpenAIDriver:
    # SDKTransport.with_options constructs another SDK client on every attach.
    _refuse_inherited_settings()
    transport = SDKTransport(sdk)

    def authorize(scope: Scope, kind: str, identity: str | None) -> bool:
        return scope == SCOPE

    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        authorization=authorize,
        journal=script.journal,
        usage_revisions=script.revisions,
        binding_lookup=lambda scope, identity: (
            script.binding if scope == SCOPE and identity == REF.id else None
        ),
    )
    driver.usage = _UsageSequence(
        Context(transport, "project", driver.capabilities().profile_id, authorize), script
    )
    script.attach(driver, sdk)
    return driver


@asynccontextmanager
async def offline_sdk_adapter(
    *, sdk_factory: SDKFactory = build_offline_sdk
) -> AsyncIterator[Adapter]:
    """Fresh fixed MockTransport with no credential, URL or real-transport options."""
    _refuse_inherited_settings()
    script = OpenAISDKScript()
    async with sdk_factory(
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(script.handle),
    ) as sdk:
        driver = _attach(script, sdk)
        pending = {
            f"C{i:02}": PendingReason(
                PendingKind.ADAPTER_DEPENDENCY,
                "This SDK component supplies only C06/C07/C09 native envelopes.",
            )
            for i in range(1, 19)
            if i not in (6, 7, 9)
        }
        pending["C08"] = PendingReason(
            PendingKind.CAPABILITY_UNAVAILABLE,
            "Conditional tool/required-mount replacement is refused before native mutation.",
        )
        pending["C12"] = PendingReason(
            PendingKind.CAPABILITY_UNAVAILABLE,
            "Neutral immutable-disjoint coverage excludes simultaneous overlapping turn/session "
            "aggregates; complete shared C12 contract not certified.",
        )
        try:
            yield Adapter(driver, script.store, script, pending=pending)
        finally:
            await script.aclose()

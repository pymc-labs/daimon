"""Opt-in real Anthropic SDK adapter for the unchanged C01–C18 runner.

This is a test adapter: the shared ScriptedTransport belongs to daimon.testing,
which production mux must never import. Native replies cross the actual SDK;
only the N9 fixtures supply verdicts. No live keys, discovery or socket fallback.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict
from types import MappingProxyType, TracebackType

import httpx
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.conformance.runner import (
    Adapter,
    PendingKind,
    PendingReason,
    Registry,
    Scenario,
    SendEvidence,
    run,
)
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import SessionSpec, SkillUpload
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.state.memory import CrashPoint
from mux.state.store import StateStore

SCRIPTED_FIXTURES = frozenset({"C15", "C16"})
PENDING: Mapping[str, PendingReason] = MappingProxyType(
    {
        "C01": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host multi-human attribution, input batching and workspace binding are not wired",
        ),
        "C02": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host clock-driven expiry/loss classification and workspace continuity adapter absent",
        ),
        "C03": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "driver passes send keys through; host durable intent, claiming and receipt replay absent",
        ),
        "C04": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host restartable StateStore, transaction crash injection and lease fencing absent",
        ),
        "C05": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "Events.reconcile is unavailable; host paged journal/gap/root projection adapter absent",
        ),
        "C06": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "native cancellation exists, but Session reads do not supply the host active-root projection",
        ),
        "C07": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "native usage spans are revision 1; host correction sequence and durable outbox bridge absent",
        ),
        "C08": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "generic resource update is refused; host next-turn preparation/reconciliation adapter absent",
        ),
        "C09": PendingReason(
            PendingKind.CAPABILITY_UNAVAILABLE,
            "binary files are supported, but Sessions.delete is unavailable; archive is not hard deletion",
        ),
        "C10": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "fixture requires memory_stores refusal, while the actual Anthropic profile declares it native; "
            "provider-appropriate admission scenario is missing",
        ),
        "C11": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "inline skills exist, but deployed MCP/repository bindings and host required-action occupancy "
            "adapter are not seeded",
        ),
        "C12": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host ledger/accounting evidence hook, overlapping-grain billing and rollback/restart absent",
        ),
        "C13": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host durable new-slot CAS adoption/restart adapter is not connected to native session bindings",
        ),
        "C14": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host existing/new-thread backend registry selection adapter absent",
        ),
        "C17": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host wake generation and lease fencing adapter absent",
        ),
        "C18": PendingReason(
            PendingKind.ADAPTER_DEPENDENCY,
            "host terminated-session outcome row persistence adapter absent",
        ),
    }
)

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="qa", authorization_id="a6-offline"
)
REF = ResourceRef(
    id="sess_a6",
    kind="session",
    provider="anthropic",
    account_scope_id="offline",
    tenant_id="tenant",
    account_id="account",
)
AUTHORIZATION = ResourceAuthorization(SCOPE, frozenset({("session", REF.id)}))
SESSION_PATH = "/v1/sessions/" + REF.id
EVENTS_PATH = SESSION_PATH + "/events"
BETA_QUERY = (("beta", "true"),)


def session_reply(*, running: bool = False) -> ScriptedReply:
    """Validate the native session shape before the SDK parses its HTTP reply."""
    return ScriptedReply(
        "GET",
        SESSION_PATH,
        httpx.Response(
            200,
            json=ma_session(
                id=REF.id,
                agent_id="ag_a6",
                model="claude-haiku-4-5-20251001",
                environment_id="env_a6",
                status="running" if running else "idle",
                metadata={
                    "daimon_tenant": "tenant",
                    "daimon_channel": "channel",
                    "daimon_thread": "thread",
                },
            ).model_dump(mode="json"),
        ),
        query=BETA_QUERY,
    )


def empty_events_reply() -> ScriptedReply:
    return ScriptedReply(
        "GET",
        EVENTS_PATH,
        httpx.Response(200, json={"data": [], "next_page": None}),
        query=BETA_QUERY,
    )


class AnthropicScript:
    """Strict wire script, independent of driver projections and receipts."""

    def __init__(self) -> None:
        self.wire = ScriptedTransport()
        self.sdk = self.wire.client()
        self.driver = AnthropicManagedAgents(
            self.sdk, account_scope_id="offline", authorization=AUTHORIZATION
        )

    async def arrange(self, fixture_id: str) -> Scenario:
        if fixture_id not in SCRIPTED_FIXTURES:
            raise ValueError("fixture has no executable Anthropic script")
        self.wire.queue(session_reply())
        session = await self.driver.sessions.retrieve(SCOPE, REF)
        if fixture_id == "C15":
            self.wire.queue(session_reply(), session_reply())
        elif fixture_id == "C16":
            self.wire.queue(empty_events_reply(), empty_events_reply())
        return Scenario(
            scope=SCOPE,
            foreign_scope=SCOPE.model_copy(update={"tenant_id": "foreign"}),
            session=session,
            desired=SessionSpec(
                agent=REF.model_copy(update={"id": "ag_a6", "kind": "agent"}),
                agent_revision=Revision(local=0),
                config_revision=0,
            ),
        )

    def fault(self, name: str) -> None:
        raise ValueError("no faults in these two executable probes")

    @property
    def mutation_count(self) -> int:
        return sum(request.method != "GET" for request in self.wire.requests)

    @property
    def deleted_resources(self) -> tuple[ResourceRef, ...]:
        return tuple(
            REF
            for request in self.wire.requests
            if request.method == "DELETE" and request.path == SESSION_PATH
        )

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
        raise RuntimeError("host StateStore bridge not connected")

    async def aclose(self) -> None:
        await self.sdk.close()


class AnthropicOfflineFactory:
    """Fresh real drivers per fixture; the context owns SDK client cleanup."""

    def __init__(self) -> None:
        self.scripts: list[AnthropicScript] = []

    def __call__(self) -> Adapter:
        script = AnthropicScript()
        self.scripts.append(script)
        return Adapter(driver=script.driver, store=None, transport=script, pending=PENDING)

    async def __aenter__(self) -> AnthropicOfflineFactory:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for script in self.scripts:
            await script.aclose()


def register(registry: Registry) -> AnthropicOfflineFactory:
    adapters = AnthropicOfflineFactory()
    registry.register("anthropic.offline", adapters)
    return adapters


async def main() -> int:
    """Emit the whole offline matrix, explicitly withholding certification."""
    registry = Registry()
    async with register(registry) as factory:
        results = await run(registry, "anthropic.offline")
        for script in factory.scripts:
            script.wire.assert_consumed()
    print(
        json.dumps(
            {
                "adapter": "anthropic.offline",
                "certified": all(result.status == "pass" for result in results),
                "results": [asdict(result) for result in results],
            },
            indent=2,
        )
    )
    return int(any(result.status == "fail" for result in results))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

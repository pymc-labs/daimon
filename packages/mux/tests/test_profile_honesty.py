"""Profile honesty probe: every supported capability is exercised on its driver.

A profile that declares a capability `native` or `emulated` promises admission
will let a channel require it. This module calls the real driver entry point
for each such capability, with minimal inputs and no network, and fails when the
driver refuses it with `UnsupportedCapability`. It also checks that each
profile's `accounting_mode` names the usage grain its driver emits, and that a
newly declared capability cannot ship without a probe.

How a probe passes: the driver call either returns, or gets as far as its
transport (the `Reached` sentinel or a normalized `ProviderError`). A refusal
(`UnsupportedCapability`, `ExtensionVersionError`) is an overclaim. Any other
error means the probe's inputs are wrong and fails loudly, so a probe cannot
hide behind an unrelated exception.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any, Literal

import httpx
import pytest
from anthropic import AsyncAnthropic
from mux.contracts.actions import UserMessage, UserToolConfirmation, UserToolResult
from mux.contracts.config import (
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    resolve_default,
)
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.ports import (
    EnvironmentsFork,
    MemoryStores,
    SessionResources,
    SkillVersions,
    Steering,
    Vaults,
)
from mux.contracts.profile import Capability, Profile
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
)
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.errors import ExtensionVersionError, ProviderError, UnsupportedCapability
from mux.profiles import PROFILES

type Grain = Literal["model_request", "turn", "session"]
type Probe = Callable[[Rig], Awaitable[object]]
type RigFactory = Callable[[], AbstractAsyncContextManager[Rig]]


class Reached(Exception):
    """A transport double was called: the driver did not refuse before I/O."""


@dataclass
class Rig:
    """One driver under test, a scope it accepts and a session it can address."""

    driver: Any
    scope: Scope
    session: ResourceRef
    harness: Any = None
    extras: dict[str, Any] = field(default_factory=dict[str, Any])


def _ref(provider: str, kind: str, id_: str, account_scope: str, scope: Scope) -> ResourceRef:
    return ResourceRef.model_validate(
        {
            "id": id_,
            "kind": kind,
            "provider": provider,
            "account_scope_id": account_scope,
            "tenant_id": scope.tenant_id,
            "account_id": scope.account_id,
        }
    )


MESSAGE = UserMessage(content=(TextPart(text="hello"),))
STEER = UserMessage(content=(TextPart(text="hello"),), mode="steer")
CONFIRMATION = UserToolConfirmation(action_id="action", decision="allow")
TOOL_RESULT = UserToolResult(action_id="action", content=(TextPart(text="ok"),))
SKILL = SkillUpload(
    files=(SkillUploadFile(path="SKILL.md", content=b"---\nname: probe\n---\nbody\n"),)
)
PAGE = PageRequest()
LEGACY_SCOPE = Scope.legacy_host_authorized(call_site="profile-honesty-probe")
SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="grant")


# --------------------------------------------------------------------------- anthropic

ANTHROPIC_SCOPE_ID = "workspace"


@asynccontextmanager
async def anthropic_rig() -> AsyncIterator[Rig]:
    """The SDK client points at a closed loopback port, so no request can leave."""
    client = AsyncAnthropic(api_key="probe", base_url="http://127.0.0.1:9", max_retries=0)
    driver = AnthropicManagedAgents(client, account_scope_id=ANTHROPIC_SCOPE_ID)
    session = _ref("anthropic", "session", "sesn_probe", ANTHROPIC_SCOPE_ID, LEGACY_SCOPE)
    try:
        yield Rig(driver, LEGACY_SCOPE, session)
    finally:
        await client.close()


@asynccontextmanager
async def anthropic_usage_rig() -> AsyncIterator[Rig]:
    """An SDK client whose only reply is one model-request span, served in process."""
    span = {
        "id": "sevt_probe",
        "type": "span.model_request_end",
        "processed_at": "2026-10-09T00:00:00Z",
        "is_error": False,
        "model_request_start_id": "sevt_start",
        "model_usage": {
            "input_tokens": 10,
            "output_tokens": 40,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 20,
            "speed": None,
        },
    }
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [span], "next_page": None})
        )
    )
    client = AsyncAnthropic(
        api_key="probe", base_url="http://anthropic.invalid", http_client=http, max_retries=0
    )
    driver = AnthropicManagedAgents(client, account_scope_id=ANTHROPIC_SCOPE_ID)
    session = _ref("anthropic", "session", "sesn_probe", ANTHROPIC_SCOPE_ID, LEGACY_SCOPE)
    try:
        yield Rig(driver, LEGACY_SCOPE, session)
    finally:
        await client.close()


def _anthropic_ext[T](rig: Rig, port: type[T], namespace: str) -> T:
    return rig.driver.extension(port, namespace=namespace, version=1)


def _anthropic_session_spec(rig: Rig) -> SessionSpec:
    return SessionSpec(
        agent=_ref("anthropic", "agent", "agent_probe", ANTHROPIC_SCOPE_ID, rig.scope),
        agent_revision=Revision(local=1, native="1"),
        environment=_ref("anthropic", "environment", "env_probe", ANTHROPIC_SCOPE_ID, rig.scope),
        config_revision=1,
    )


async def _anthropic_multiagent(rig: Rig) -> object:
    spec = AgentSpec(
        name="probe",
        model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
        extensions=(
            ExtensionConfig(
                namespace="anthropic.multiagent",
                version=1,
                value={"type": "coordinator", "agents": [{"type": "self"}]},
            ),
        ),
    )
    return await rig.driver.agents.create(rig.scope, spec, key="multiagent")


def _anthropic_workspace_export(rig: Rig) -> Awaitable[object]:
    from mux.drivers.anthropic.sessions_lifecycle import WorkspaceTransfer

    async def run() -> object:
        transfer = _anthropic_ext(rig, WorkspaceTransfer, "anthropic.workspace_transfer")
        return transfer.export(
            rig.scope,
            rig.session,
            inline=ExtensionConfig(
                namespace="anthropic.workspace_transfer",
                version=1,
                value={"outcome": {"type": "history", "gap_reason": "events_unavailable"}},
            ),
            key="export",
        )

    return run()


async def _anthropic_spend_limit(rig: Rig) -> object:
    spec = _anthropic_session_spec(rig).model_copy(
        update={
            "extensions": {
                "anthropic.session_create": ExtensionConfig(
                    namespace="anthropic.session_create",
                    version=1,
                    value={
                        "budget": {
                            "type": "limit",
                            "max_list_cost": {"amount": "1", "currency": "USD"},
                        }
                    },
                )
            }
        }
    )
    return await rig.driver.sessions.create(rig.scope, spec, key="budget")


ANTHROPIC_PROBES: dict[Capability, Probe] = {
    "thread_workspace_persistence": lambda r: r.driver.sessions.create(
        r.scope, _anthropic_session_spec(r), key="create"
    ),
    "turn_lifecycle": lambda r: r.driver.events.send(r.scope, r.session, (MESSAGE,), key="send"),
    "cancel": lambda r: r.driver.events.cancel(r.scope, r.session, turn_id="turn", key="cancel"),
    "tool_loop": lambda r: r.driver.events.send(r.scope, r.session, (TOOL_RESULT,), key="result"),
    "required_actions": lambda r: r.driver.sessions.retrieve(r.scope, r.session),
    "skills_bundle": lambda r: r.driver.skills.create(r.scope, SKILL, key="skill"),
    "artifacts": lambda r: r.driver.artifacts.list(r.scope, r.session, page=PAGE),
    "usage_observations": lambda r: r.driver.usage.reconcile(r.scope, r.session),
    "reconcile": lambda r: r.driver.events.reconcile(r.scope, r.session),
    "tool_confirmation": lambda r: r.driver.events.send(
        r.scope, r.session, (CONFIRMATION,), key="confirm"
    ),
    "event_previews": lambda r: r.driver.events.open_stream(r.scope, r.session, previews=True),
    "vaults": lambda r: _anthropic_ext(r, Vaults, "anthropic.vaults").list(r.scope, page=PAGE),
    "memory_stores": lambda r: _anthropic_ext(r, MemoryStores, "anthropic.memory_stores").list(
        r.scope, page=PAGE
    ),
    "session_resources": lambda r: _anthropic_ext(
        r, SessionResources, "anthropic.session_resources"
    ).list(r.scope, r.session),
    "skills_versions": lambda r: _anthropic_ext(
        r, SkillVersions, "anthropic.skills_versions"
    ).versions(r.scope, "skill_probe", page=PAGE),
    "multiagent": _anthropic_multiagent,
    "model_request_usage": lambda r: r.driver.usage.reconcile(r.scope, r.session),
    "session_spend_control": _anthropic_spend_limit,
    "workspace_export_import": _anthropic_workspace_export,
}
# Capabilities the profile does not claim because the driver refuses them. Each
# probe must keep being refused; implementing one means declaring it and moving
# the probe into the table above. `native_schedules` has no driver entry point.
ANTHROPIC_REFUSED: dict[Capability, Probe] = {
    "steer": lambda r: r.driver.events.send(r.scope, r.session, (STEER,), key="steer"),
    "native_event_replay": lambda r: r.driver.events.open_stream(
        r.scope, r.session, after="evt_probe"
    ),
    "environments_fork": lambda r: _anthropic_ext(
        r, EnvironmentsFork, "anthropic.environments_fork"
    ).fork(
        r.scope,
        _ref("anthropic", "environment", "env_probe", ANTHROPIC_SCOPE_ID, r.scope),
        ExtensionConfig(namespace="anthropic.environments_fork", version=1),
        key="fork",
    ),
}


# --------------------------------------------------------------------------- openai


class OpenAIWire:
    """Transport double: answers scripted reads, records writes, then reaches."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.replies: dict[tuple[str, str], dict[str, Any]] = {}

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        self.requests.append((method, path, dict(body) if body is not None else None))
        if (method, path) in self.replies:
            return self.replies[(method, path)]
        raise Reached(f"{method} {path}")

    async def open_stream(self, path: str) -> AsyncIterator[dict[str, Any]]:
        self.requests.append(("STREAM", path, None))
        raise Reached(f"STREAM {path}")

    def script_session(self, *, workspace: bool, status: str = "idle", actions: Any = ()) -> None:
        """Answer the reads a send needs: the session, and its active root turn."""
        session = {
            "id": "sess_probe",
            "agent": {"id": "agent_probe", "model": "probe"},
            "created_at": 0,
            "environment": {"type": "openai_hosted", "id": "env"}
            if workspace
            else {"type": "none"},
            "status": status,
            "required_actions": list(actions),
            "metadata": {"mux_tenant": SCOPE.tenant_id},
        }
        turn = {
            "id": "root",
            "session_id": "sess_probe",
            "agent_id": "agent_probe",
            "subagent_id": None,
            "status": "in_progress",
            "created_at": 0,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        self.replies[("GET", "/agents/sessions/sess_probe")] = session
        self.replies[("GET", "/agents/sessions/sess_probe/turns")] = {
            "data": [turn],
            "has_more": False,
            "last_id": None,
        }
        self.replies[("POST", "/agents/sessions/sess_probe/events")] = {}

    async def multipart(
        self,
        path: str,
        *,
        files: tuple[tuple[str, str, bytes | IO[bytes], str], ...],
        fields: Mapping[str, str],
        key: str,
    ) -> dict[str, Any]:
        self.requests.append(("MULTIPART", path, None))
        raise Reached(f"MULTIPART {path}")

    async def download(self, path: str) -> AsyncIterator[bytes]:
        self.requests.append(("DOWNLOAD", path, None))
        raise Reached(f"DOWNLOAD {path}")


OPENAI_SCOPE_ID = "project"


def openai_rig_for(
    profile_id: str, **options: Any
) -> Callable[[], AbstractAsyncContextManager[Rig]]:
    @asynccontextmanager
    async def rig() -> AsyncIterator[Rig]:
        from mux.drivers.openai import OpenAIDriver
        from mux.drivers.openai.turn import MemoryRecoveryJournal
        from mux.drivers.openai.usage import MemoryUsageRevisions

        wire = OpenAIWire()
        driver = OpenAIDriver(
            wire,  # type: ignore[arg-type]
            account_scope_id=OPENAI_SCOPE_ID,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            profile_id=profile_id,
            authorization=lambda scope, kind, id_: scope == SCOPE,
            **options,
        )
        session = _ref("openai", "session", "sess_probe", OPENAI_SCOPE_ID, SCOPE)
        yield Rig(
            driver,
            SCOPE,
            session,
            harness=wire,
            extras={"workspace": profile_id.endswith("workspace")},
        )

    return rig


def _openai_session_spec(rig: Rig, **update: Any) -> SessionSpec:
    return SessionSpec(
        agent=_ref("openai", "agent", "agent_probe", OPENAI_SCOPE_ID, rig.scope),
        agent_revision=Revision(local=0),
        config_revision=1,
        **update,
    )


def _openai_ext[T](rig: Rig, port: type[T], namespace: str) -> T:
    return rig.driver.extension(port, namespace=namespace, version=1)


FUNCTION_CALL = {
    "type": "function_call",
    "call_id": "action",
    "turn_id": "root",
    "name": "tool",
    "arguments": {},
}


async def _openai_send(rig: Rig) -> object:
    rig.harness.script_session(workspace=rig.extras["workspace"])
    return await rig.driver.events.send(rig.scope, rig.session, (MESSAGE,), key="send")


async def _openai_steer(rig: Rig) -> object:
    rig.harness.script_session(workspace=rig.extras["workspace"], status="in_progress")
    steering = _openai_ext(rig, Steering, "openai.steer")
    receipt = await steering.steer(rig.scope, rig.session, MESSAGE, active_turn="root", key="steer")
    assert receipt.status == "queued"
    return receipt


async def _openai_tool_loop(rig: Rig) -> object:
    rig.harness.script_session(
        workspace=rig.extras["workspace"], status="requires_action", actions=(FUNCTION_CALL,)
    )
    return await rig.driver.events.send(rig.scope, rig.session, (TOOL_RESULT,), key="result")


async def _openai_required_action(rig: Rig) -> object:
    rig.harness.script_session(
        workspace=rig.extras["workspace"], status="requires_action", actions=(FUNCTION_CALL,)
    )
    session = await rig.driver.sessions.retrieve(rig.scope, rig.session)
    assert session.state == "requires_action" and session.required_actions
    return session


async def _openai_usage(rig: Rig) -> object:
    rig.harness.script_session(workspace=rig.extras["workspace"])
    observations = await rig.driver.usage.reconcile(rig.scope, rig.session)
    assert observations and observations[0].completeness == "measured"
    return observations


def _openai_probes(*, workspace: bool) -> dict[Capability, Probe]:
    spec: Callable[[Rig], SessionSpec] = (
        (lambda r: _openai_session_spec(r)) if workspace else (lambda r: _openai_session_spec(r))
    )
    probes: dict[Capability, Probe] = {
        "neutral_catalog": lambda r: r.driver.agents.create(
            r.scope,
            AgentSpec(name="probe", model=ModelRef(provider="openai", id="gpt-6-luna")),
            key="agent",
        ),
        "agent_delete": lambda r: r.driver.agents.delete(
            r.scope,
            _ref("openai", "agent", "agent_probe", OPENAI_SCOPE_ID, r.scope),
            key="delete",
        ),
        "turn_lifecycle": _openai_send,
        "cancel": lambda r: r.driver.events.cancel(r.scope, r.session, turn_id="t", key="c"),
        "tool_loop": _openai_tool_loop,
        "required_actions": _openai_required_action,
        "usage_observations": _openai_usage,
        "reconcile": lambda r: r.driver.events.reconcile(r.scope, r.session),
        "steer": _openai_steer,
        "event_previews": lambda r: r.driver.events.open_stream(r.scope, r.session, previews=True),
        "artifacts": lambda r: r.driver.artifacts.list(r.scope, r.session, page=PAGE),
        "vaults": lambda r: _openai_ext(r, Vaults, "openai.vaults").list(r.scope, page=PAGE),
    }
    if workspace:
        probes["thread_workspace_persistence"] = lambda r: r.driver.sessions.create(
            r.scope, spec(r), key="create"
        )
        probes["skills_bundle"] = lambda r: r.driver.skills.create(r.scope, SKILL, key="skill")
        probes["skills_versions"] = lambda r: r.driver.skills.publish_version(
            r.scope, "skill_probe", SKILL, key="version"
        )
    return probes


# --------------------------------------------------------------------------- gemini


def gemini_interaction(id_: str = "i1", status: str = "completed", **extra: Any) -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": id_,
        "status": status,
        "created": stamp,
        "updated": stamp,
        "environment_id": "e1",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "done"}]}],
        "usage": {
            "total_input_tokens": 10,
            "total_cached_tokens": 2,
            "total_output_tokens": 20,
            "total_thought_tokens": 3,
        },
        **extra,
    }


class GeminiWire:
    """Scripted Gemini transport: queued create replies, saved interactions."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.saved: dict[str, dict[str, Any]] = {}

    async def create(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.requests.append(dict(request))
        reply = self.responses.pop(0)
        self.saved[reply["id"]] = reply
        return reply

    async def get(self, interaction_id: str) -> dict[str, Any]:
        return self.saved[interaction_id]

    async def cancel(self, interaction_id: str) -> dict[str, Any]:
        return self.saved[interaction_id]

    async def download_snapshot(self, environment_id: str) -> bytes:
        raise Reached(f"snapshot {environment_id}")

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        raise Reached(f"stream {interaction_id}")

    async def count_input_tokens(self, request: Mapping[str, Any]) -> int:
        return 5


class GeminiStorage:
    """In-memory transactional storage; one shared Records object."""

    def __init__(self) -> None:
        from mux.drivers.gemini.storage import Records

        self.records = Records()

    @asynccontextmanager
    async def transaction(self, *, after_dispatch: bool = False) -> AsyncIterator[Any]:
        yield self.records


GEMINI_THREAD = {
    "thread": {
        "channel": {"tenant_id": "t", "platform": "probe", "channel_id": "c"},
        "thread_id": "th",
    },
    "binding_id": "b",
}


async def _gemini_session(driver: Any, scope: Scope, *, workspace: bool = True) -> ResourceRef:
    agent = await driver.agents.create(
        scope,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    env = (
        await driver.environments.create(scope, EnvironmentSpec(name="e"), key="env")
        if workspace
        else None
    )
    session = await driver.sessions.create(
        scope,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=env.ref if env is not None else None,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session", version=1, value=GEMINI_THREAD
                )
            },
        ),
        key="session",
    )
    return session.ref


def gemini_rig_for(
    *, workspace: bool = True, **options: Any
) -> Callable[[], AbstractAsyncContextManager[Rig]]:
    @asynccontextmanager
    async def rig() -> AsyncIterator[Rig]:
        from mux.drivers.gemini import GeminiManagedAgents
        from mux.state.memory import MemoryStateStore

        wire, storage, state = GeminiWire(), GeminiStorage(), MemoryStateStore()
        driver = GeminiManagedAgents(
            wire,  # type: ignore[arg-type]
            storage=storage,  # type: ignore[arg-type]
            state_store=state,
            account_scope_id="project",
            **options,
        )
        session = await _gemini_session(driver, SCOPE, workspace=workspace)
        yield Rig(driver, SCOPE, session, harness=wire, extras={"state": state})

    return rig


async def _gemini_send(
    rig: Rig, *events: Any, reply: dict[str, Any] | None = None, key: str = "probe-send"
) -> object:
    rig.harness.responses.append(reply or gemini_interaction())
    return await rig.driver.events.send(rig.scope, rig.session, events, key=key)


def _gemini_function_call() -> dict[str, Any]:
    return gemini_interaction(
        status="requires_action",
        steps=[{"type": "function_call", "id": "call", "name": "weather", "arguments": {"c": "x"}}],
    )


async def _gemini_required_action(rig: Rig) -> object:
    await _gemini_send(rig, MESSAGE, reply=_gemini_function_call())
    session = await rig.driver.sessions.retrieve(rig.scope, rig.session)
    assert session.required_actions and session.required_actions[0].call_id == "call"
    return session


async def _gemini_tool_loop(rig: Rig) -> object:
    await _gemini_send(rig, MESSAGE, reply=_gemini_function_call())
    result = UserToolResult(action_id="call", content=(TextPart(text="cold"),))
    return await _gemini_send(rig, result, reply=gemini_interaction("i2"), key="probe-result")


async def _gemini_usage(rig: Rig) -> object:
    await _gemini_send(rig, MESSAGE)
    return await rig.driver.usage.reconcile(rig.scope, rig.session)


async def _gemini_output_limit(rig: Rig) -> object:
    """The ordinary model mode compiles the output ceiling into the request."""
    inner = gemini_rig_for(workspace=False, max_output_tokens=64)
    async with inner() as limited:
        await _gemini_send(limited, MESSAGE)
        request = limited.harness.requests[0]
        assert request["generation_config"] == {"max_output_tokens": 64}
        return request


GEMINI_PROBES: dict[Capability, Probe] = {
    "neutral_catalog": lambda r: r.driver.agents.create(
        r.scope,
        AgentSpec(name="b", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="catalog",
    ),
    "output_token_limit": _gemini_output_limit,
    "turn_lifecycle": lambda r: _gemini_send(r, MESSAGE),
    "cancel": lambda r: r.driver.events.cancel(r.scope, r.session, turn_id="t", key="cancel"),
    "tool_loop": _gemini_tool_loop,
    "required_actions": _gemini_required_action,
    "usage_observations": _gemini_usage,
    "skills_bundle": lambda r: r.driver.skills.create(r.scope, SKILL, key="skill"),
    "artifacts": lambda r: r.driver.artifacts.list(r.scope, r.session, page=PAGE),
    "reconcile": lambda r: r.driver.events.reconcile(r.scope, r.session),
    "event_previews": lambda r: r.driver.events.open_stream(r.scope, r.session, previews=True),
}


# --------------------------------------------------------------------------- selfhosted


class Harness:
    """In-process C3 harness: durable event log, one turn per idempotency key."""

    def __init__(self, mode: str = "complete") -> None:
        self.mode = mode
        self.events: list[dict[str, Any]] = []
        self.requests: dict[str, dict[str, Any]] = {}
        self.turn: dict[str, Any] = {}

    def emit(self, type_: str, data: dict[str, Any]) -> None:
        if type_ in ("agent_message", "agent_message_delta"):
            data = {"message_id": self.turn["turn_id"] + ":message", **data}
        self.events.append(
            {
                "seq": len(self.events) + 1,
                "session_id": self.turn["session_id"],
                "turn_id": self.turn["turn_id"],
                "at": datetime.now(UTC).isoformat(),
                "type": type_,
                "data": data,
            }
        )

    def finish(self, outcome: str = "completed") -> None:
        self.emit("agent_message", {"parts": [{"type": "text", "text": "done"}]})
        self.emit("turn_ended", {"outcome": outcome})

    def start(self, body: dict[str, Any]) -> None:
        self.turn = body
        self.emit("status_running", {})
        self.emit(
            "usage",
            {
                "request_id": "request-1",
                "model": body["model"],
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 5,
                "reasoning_tokens": 1,
            },
        )
        if self.mode == "approval":
            self.emit(
                "requires_action",
                {
                    "action_id": "approval",
                    "kind": "tool_confirmation",
                    "call_id": "call",
                    "name": "bash",
                    "input": {"command": "echo hi"},
                },
            )
        elif self.mode != "running":
            self.emit("agent_message_delta", {"text": "do"})
            self.emit(
                "tool_use",
                {
                    "call_id": "call",
                    "name": "bash",
                    "input": {"command": "ls"},
                    "executor": "agent",
                },
            )
            self.emit("tool_result", {"call_id": "call", "output": "hi", "is_error": False})
            self.finish()

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path.endswith("/events"):
            session_id = path.split("/")[-2]
            values = [e for e in self.events if e["session_id"] == session_id]
            after = int(request.url.params.get("after_seq", 0))
            return httpx.Response(
                200,
                json={
                    "events": [e for e in values if e["seq"] > after],
                    "complete": not values or values[-1]["type"] == "turn_ended",
                },
            )
        if path == "/v0/turns":
            if body["idempotency_key"] not in self.requests:
                self.requests[body["idempotency_key"]] = body
                self.start(body)
            return httpx.Response(202, json={"accepted": True, "turn_id": body["turn_id"]})
        if path.endswith("/actions"):
            self.emit("status_running", {})
            self.finish()
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/cancel"):
            self.finish("interrupted")
            return httpx.Response(200, json={"cancelled": True})
        raise AssertionError(path)


class StaticResource:
    """A resource port that only answers `retrieve` with one fixed record."""

    def __init__(self, value: Any) -> None:
        self.value = value

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Any:
        return self.value


def selfhosted_rig_for(mode: str) -> Callable[[], AbstractAsyncContextManager[Rig]]:
    @asynccontextmanager
    async def rig() -> AsyncIterator[Rig]:
        from mux.contracts.resources import Agent, Environment
        from mux.drivers.selfhosted import SelfHostedDriver
        from mux.drivers.selfhosted.transport import DirectTransport

        harness = Harness(mode)
        agent_ref = _ref("selfhosted", "agent", "agent", "tenant", SCOPE).model_copy(
            update={"account_id": None}
        )
        env_ref = agent_ref.model_copy(update={"id": "environment", "kind": "environment"})
        now = datetime.now(UTC)
        agent = Agent(
            ref=agent_ref,
            revision=Revision(local=1),
            spec=AgentSpec(
                name="probe", model=ModelRef(provider="selfhosted", id="gpt-6-luna"), system="s"
            ),
            created_at=now,
        )
        environment = Environment(
            ref=env_ref,
            revision=Revision(local=1),
            spec=EnvironmentSpec(name="probe", execution="self_hosted"),
            created_at=now,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
            driver = SelfHostedDriver(
                DirectTransport(client, base_url="https://harness", token="probe-token"),
                account_scope_id="tenant",
                agents=StaticResource(agent),  # type: ignore[arg-type]
                environments=StaticResource(environment),  # type: ignore[arg-type]
                authorization=lambda scope, kind, id_: scope == SCOPE,
                deployment="gpt-6-luna",
            )
            session = await driver.sessions.create(
                SCOPE,
                SessionSpec(
                    agent=agent_ref,
                    agent_revision=Revision(local=1),
                    environment=env_ref,
                    config_revision=1,
                ),
                key="create",
            )
            yield Rig(driver, SCOPE, session.ref, harness=harness)

    return rig


async def _selfhosted_send(rig: Rig, *events: Any, **options: Any) -> Any:
    return await rig.driver.events.send(rig.scope, rig.session, events, key="send", **options)


async def _selfhosted_stream(rig: Rig, **options: Any) -> list[Any]:
    await _selfhosted_send(rig, MESSAGE)
    stream = await rig.driver.events.open_stream(rig.scope, rig.session, **options)
    return [event async for event in stream]


async def _selfhosted_tool_loop(rig: Rig) -> object:
    events = await _selfhosted_stream(rig)
    assert {"agent.tool_use", "agent.tool_result"} <= {e.type for e in events}
    return events


async def _selfhosted_previews(rig: Rig) -> object:
    events = await _selfhosted_stream(rig, previews=True)
    assert "agent.message.delta" in {e.type for e in events}
    return events


async def _selfhosted_replay(rig: Rig) -> object:
    events = await _selfhosted_stream(rig, after="0")
    assert events and events[-1].type == "session.turn_ended"
    return events


async def _selfhosted_cancel(rig: Rig) -> object:
    sent = await _selfhosted_send(rig, MESSAGE)
    return await rig.driver.events.cancel(rig.scope, rig.session, turn_id=sent.turn_id, key="c")


async def _selfhosted_required_action(rig: Rig) -> object:
    await _selfhosted_send(rig, MESSAGE)
    snapshot = await rig.driver.events.reconcile(rig.scope, rig.session)
    assert snapshot.required_actions and snapshot.required_actions[0].id == "approval"
    return snapshot


async def _selfhosted_confirmation(rig: Rig) -> object:
    await _selfhosted_send(rig, MESSAGE)
    return await rig.driver.events.send(
        rig.scope,
        rig.session,
        (UserToolConfirmation(action_id="approval", decision="allow"),),
        key="approve",
    )


async def _selfhosted_usage(rig: Rig) -> object:
    await _selfhosted_send(rig, MESSAGE)
    observations = await rig.driver.usage.reconcile(rig.scope, rig.session)
    assert observations and observations[0].completeness == "measured"
    return observations


SELFHOSTED_PROBES: dict[Capability, Probe] = {
    "turn_lifecycle": lambda r: _selfhosted_send(r, MESSAGE),
    "cancel": _selfhosted_cancel,
    "tool_loop": _selfhosted_tool_loop,
    "required_actions": _selfhosted_required_action,
    "usage_observations": _selfhosted_usage,
    "model_request_usage": _selfhosted_usage,
    "reconcile": lambda r: r.driver.events.reconcile(r.scope, r.session),
    "native_event_replay": _selfhosted_replay,
    "event_previews": _selfhosted_previews,
    "tool_confirmation": _selfhosted_confirmation,
}

# The rig a profile's probes run against; approval-mode probes pick their own harness.
RIGS: dict[str, RigFactory] = {
    "anthropic.managed_agents": anthropic_rig,
    "openai.persistent_workspace": openai_rig_for("openai.persistent_workspace"),
    "openai.conversation_only": openai_rig_for("openai.conversation_only"),
    "gemini.inline_reuse": gemini_rig_for(),
    "selfhosted.loop": selfhosted_rig_for("complete"),
}
RIG_OVERRIDES: dict[tuple[str, Capability], RigFactory] = {
    ("selfhosted.loop", "required_actions"): selfhosted_rig_for("approval"),
    ("selfhosted.loop", "tool_confirmation"): selfhosted_rig_for("approval"),
    ("selfhosted.loop", "cancel"): selfhosted_rig_for("running"),
}
REFUSED: dict[str, dict[Capability, Probe]] = {"anthropic.managed_agents": ANTHROPIC_REFUSED}
PROBES: dict[str, dict[Capability, Probe]] = {
    "anthropic.managed_agents": ANTHROPIC_PROBES,
    "openai.persistent_workspace": _openai_probes(workspace=True),
    "openai.conversation_only": _openai_probes(workspace=False),
    "gemini.inline_reuse": GEMINI_PROBES,
    "selfhosted.loop": SELFHOSTED_PROBES,
}

# Capabilities no offline driver call can show, with the reason. Keep this short.
JUSTIFIED: dict[tuple[str, Capability], str] = {}
# Known overclaims that cannot be fixed by editing the profile. Each is an expected
# failure that turns into a test error as soon as the driver honours it, so the entry
# must then be removed.
PENDING_OVERCLAIMS: dict[tuple[str, Capability], str] = {}


# --------------------------------------------------------------------------- usage grain

USAGE_SOURCES: dict[str, tuple[RigFactory, Probe]] = {
    "anthropic.managed_agents": (
        anthropic_usage_rig,
        lambda r: r.driver.usage.reconcile(r.scope, r.session),
    ),
    "openai.persistent_workspace": (RIGS["openai.persistent_workspace"], _openai_usage),
    "openai.conversation_only": (RIGS["openai.conversation_only"], _openai_usage),
    "gemini.inline_reuse": (RIGS["gemini.inline_reuse"], _gemini_usage),
    "selfhosted.loop": (RIGS["selfhosted.loop"], _selfhosted_usage),
}


# --------------------------------------------------------------------------- the checks

SUPPORTED_LEVELS = ("native", "emulated")


def supported_capabilities(profile: Profile) -> list[Capability]:
    return sorted(c for c, level in profile.support.items() if level in SUPPORTED_LEVELS)


CASES = [
    pytest.param(
        profile_id,
        capability,
        id=f"{profile_id}-{capability}",
        marks=(
            [pytest.mark.xfail(strict=True, reason=PENDING_OVERCLAIMS[profile_id, capability])]
            if (profile_id, capability) in PENDING_OVERCLAIMS
            else []
        ),
    )
    for profile_id, profile in PROFILES.items()
    for capability in supported_capabilities(profile)
    if (profile_id, capability) not in JUSTIFIED
]


@pytest.mark.parametrize(("profile_id", "capability"), CASES)
async def test_supported_capability_is_not_refused_by_its_driver(
    profile_id: str, capability: Capability
) -> None:
    probe = PROBES.get(profile_id, {}).get(capability)
    assert probe is not None, f"{profile_id} declares {capability} with no probe or justification"
    async with RIG_OVERRIDES.get((profile_id, capability), RIGS[profile_id])() as rig:
        try:
            result = probe(rig)
            if hasattr(result, "__await__"):
                await result
        except (UnsupportedCapability, ExtensionVersionError) as refusal:
            pytest.fail(f"{profile_id} declares {capability} but its driver refuses it: {refusal}")
        except (Reached, ProviderError):
            pass


@pytest.mark.parametrize("profile_id", list(PROFILES))
async def test_accounting_mode_names_the_usage_grain_the_driver_emits(profile_id: str) -> None:
    rig_factory, probe = USAGE_SOURCES[profile_id]
    async with rig_factory() as rig:
        observations: tuple[UsageObservation, ...] = await probe(rig)  # type: ignore[assignment]
    assert observations, f"{profile_id} produced no usage observation to classify"
    declared = PROFILES[profile_id].accounting_mode.usage_grain
    assert {o.grain for o in observations} == {declared}, (
        f"{profile_id} declares {PROFILES[profile_id].accounting_mode} "
        f"but its driver emits {sorted({o.grain for o in observations})} observations"
    )


@pytest.mark.parametrize(
    ("profile_id", "capability"),
    [
        pytest.param(profile_id, capability, id=f"{profile_id}-{capability}")
        for profile_id, probes in REFUSED.items()
        for capability in probes
    ],
)
async def test_unclaimed_capability_is_still_refused_by_its_driver(
    profile_id: str, capability: Capability
) -> None:
    """A profile may only drop a claim while the driver really refuses it."""
    assert PROFILES[profile_id].support_for(capability) not in SUPPORTED_LEVELS
    async with RIGS[profile_id]() as rig:
        with pytest.raises((UnsupportedCapability, ExtensionVersionError)):
            await REFUSED[profile_id][capability](rig)


def unprobed(profile: Profile) -> list[Capability]:
    """Supported capabilities with neither a probe nor a justification."""
    probes = PROBES.get(profile.profile_id, {})
    return [
        c
        for c in supported_capabilities(profile)
        if c not in probes and (profile.profile_id, c) not in JUSTIFIED
    ]


def test_every_profile_has_a_rig_and_probe_table() -> None:
    assert set(PROFILES) == set(RIGS) == set(PROBES) == set(USAGE_SOURCES)


def test_a_newly_declared_capability_needs_a_probe() -> None:
    profile = PROFILES["selfhosted.loop"]
    assert unprobed(profile) == []
    widened = profile.model_copy(update={"support": {**profile.support, "vaults": "native"}})
    assert unprobed(widened) == ["vaults"]


def test_probe_and_justification_tables_hold_no_stale_entries() -> None:
    for profile_id, probes in PROBES.items():
        assert set(probes) <= set(supported_capabilities(PROFILES[profile_id])), profile_id
    for profile_id, capability in JUSTIFIED:
        assert capability in supported_capabilities(PROFILES[profile_id])


@pytest.mark.parametrize("capability", ["steer", "native_event_replay", "environments_fork"])
async def test_a_dropped_claim_is_refused_at_admission_not_in_the_driver(
    capability: Capability,
) -> None:
    """A required capability the driver cannot do never reaches the driver."""
    config = ConfigRevision.create(
        ChannelRef(tenant_id="t", platform="probe", channel_id="c"),
        1,
        resolve_default(
            BackendConfig(requires={capability: CapabilityRequirement(level="required")})
        ),
    )
    async with anthropic_rig() as rig:
        with pytest.raises(UnsupportedCapability) as caught:
            rig.driver.admit(config)
    assert caught.value.missing == (capability,)
    assert caught.value.profile == "anthropic.managed_agents"

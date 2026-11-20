"""Every hosted driver self-hosts only eval environments and gives eval agents no web tools."""

from __future__ import annotations

import json

import pytest
from mux.contracts.ids import ModelRef, Provider, Revision
from mux.contracts.resources import AgentSpec, EnvironmentSpec, Purpose, SessionSpec, ToolSpec
from mux.drivers.anthropic.resources.agents import agent_payload
from mux.drivers.anthropic.resources.environments import environment_payload
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.core import compile_agent
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import Object
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport as GeminiTransport
from mux.testing.drivers.gemini.fake import MemoryStorage

from .openai.conftest import SCOPE, native_session
from .openai.conftest import FakeTransport as OpenAITransport

WEB = ("web_search", "web_fetch", "google_search", "url_context")


def _env(purpose: Purpose) -> EnvironmentSpec:
    return EnvironmentSpec(name="sandbox", execution="self_hosted", purpose=purpose)


def _agent(
    provider: Provider, purpose: Purpose, names: tuple[str, ...] = (*WEB, "bash")
) -> AgentSpec:
    tools = tuple(ToolSpec(name=n, kind="builtin") for n in names)
    return AgentSpec(
        name="a", model=ModelRef(provider=provider, id="fixture"), tools=tools, purpose=purpose
    )


def test_anthropic() -> None:
    with pytest.raises(UnsupportedCapability):
        environment_payload(_env("agent"))
    assert environment_payload(_env("eval"))["config"] == {"type": "self_hosted"}, "eval only"
    tools = json.dumps(agent_payload(_agent("anthropic", "eval"))["tools"])
    assert '"name": "web_search", "enabled": false' in tools, "web search off"
    assert '"name": "web_fetch", "enabled": false' in tools, "web fetch off"


async def test_gemini() -> None:
    transport = GeminiTransport()
    ma = GeminiManagedAgents(
        transport, storage=MemoryStorage(), state_store=MemoryStateStore(), account_scope_id="p"
    )
    with pytest.raises(UnsupportedCapability):
        await ma.environments.create(SCOPE, _env("agent"), key="agent")
    assert not transport.environments, "refused before the provider is called"
    env = await ma.environments.create(SCOPE, _env("eval"), key="eval")
    assert env.native == {"id": "environment-1", "environment_key": "key-environment-1"}
    assert transport.environments["environment-1"] == {"mode": "self_hosted"}
    again = await ma.environments.create(SCOPE, _env("eval"), key="eval")
    assert again.native == {"id": "environment-1"}, "the worker key is returned once"
    assert len(transport.environments) == 1, "a retried key makes no second environment"
    names = ("google_search", "url_context", "code_execution")
    sent = json.dumps(compile_agent(_agent("gemini", "eval", names))["tools"])
    assert "google_search" not in sent and "url_context" not in sent, "no Google web tools"
    assert "url_context" in json.dumps(compile_agent(_agent("gemini", "agent", names))), "kept"


async def test_openai() -> None:
    transport = OpenAITransport()
    driver = OpenAIDriver(
        transport,
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda s, k, i: s == SCOPE,
    )
    with pytest.raises(UnsupportedCapability):
        await driver.environments.create(SCOPE, _env("agent"), key="agent")
    env = await driver.environments.create(SCOPE, _env("eval"), key="eval")
    native: Object = {"id": "a", "created_at": 0, "model": "fixture", "name": "a"}
    native["metadata"] = {"mux_tenant": "t"}
    transport.responses["POST", "/agents"] = transport.responses["GET", "/agents/a"] = native
    agent = await driver.agents.create(SCOPE, _agent("openai", "eval"), key="agent")
    sent = json.dumps(transport.calls[-1][2])
    assert not any(name in sent for name in WEB), "no provider web tools"
    transport.responses["POST", "/agents/sessions"] = native_session()
    spec = SessionSpec(
        agent=agent.ref, agent_revision=Revision(local=0), environment=env.ref, config_revision=1
    )
    await driver.sessions.create(SCOPE, spec, key="s")
    body = transport.calls[-1][2]
    assert body is not None and body["environment"] == {
        "type": "self_hosted",
        "workspace_directory": "/workspace",
    }, "the session runs on our worker"

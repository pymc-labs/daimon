"""The hosted backend table loads one provider module on first use, never by default."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from daimon.core.mux_backend import TurnRuntime
from daimon.core.turn import runtimes
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.runtimes import HostedBackend
from mux.errors import ScopeViolation, UnsupportedCapability

from .test_provider_dispatch import PROFILE, REVISION, SCOPE, admission


def _forbidden_import(name):
    raise AssertionError(f"provider module {name} was imported")


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(runtimes, "_BACKENDS", {})
    return runtimes


def selected_request(**changes):
    return SimpleNamespace(
        admission=admission(),
        deps=SimpleNamespace(turn_path="mux", channel_backends=True),
        scope=SCOPE,
        platform="slack",
        tenant_id=REVISION.channel.tenant_id,
        session_account_id=SCOPE.account_id,
        thread_id="thread",
        **changes,
    )


def _cold(code):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH"
        and not key.startswith(("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_"))
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_every_hosted_profile_loads_in_a_cold_process():
    _cold("""
from daimon.core.turn import runtimes

assert not runtimes._BACKENDS
for profile in runtimes.hosted_profiles():
    backend = runtimes.hosted_backend(profile)
    hooks = (backend.runtime, backend.backend, backend.codec, backend.prepare, backend.reader)
    assert None not in hooks, profile
    assert backend.tariffs is not None, profile
for profile in ("openai.persistent_workspace", "selfhosted.loop"):
    assert runtimes.hosted_backend(profile).authorizations is not None, profile
assert runtimes.hosted_backend("openai.conversation_only") is None
""")


def test_default_profile_never_imports_a_provider_module(registry, monkeypatch):
    monkeypatch.setattr(registry, "import_module", _forbidden_import)
    assert registry.hosted_backend("anthropic.managed_agents") is None
    request = selected_request()
    request.admission = replace(request.admission, backend_revision=None)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        registry.build_channel_runtime(request)
    assert registry._BACKENDS == {}


def test_an_injected_backend_is_used_without_importing_the_provider(registry, monkeypatch):
    runtime = TurnRuntime(lambda config, scope: object(), object(), object())
    calls = []

    def factory(request):
        calls.append(request)
        return runtime

    monkeypatch.setattr(registry, "import_module", _forbidden_import)
    registry._BACKENDS[PROFILE] = HostedBackend(runtime=factory)
    request = selected_request()
    assert registry.build_channel_runtime(request) is runtime
    assert calls == [request]


def test_an_unloadable_provider_refuses_without_a_provider_call(registry, monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(registry, "import_module", missing)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        registry.build_channel_runtime(selected_request())


@pytest.mark.parametrize(
    "invalid", ["legacy", "disabled", "foreign_scope", "foreign_provider", "missing_model"]
)
def test_invalid_turn_refuses_before_loading_a_provider(registry, monkeypatch, invalid):
    request = selected_request()
    if invalid == "legacy":
        request.deps.turn_path = "legacy"
    elif invalid == "disabled":
        request.deps.channel_backends = False
    elif invalid == "foreign_scope":
        request.scope = SCOPE.model_copy(update={"tenant_id": "foreign"})
    elif invalid == "foreign_provider":
        request.admission = replace(
            request.admission, backend_revision=REVISION.model_copy(update={"backend": "gemini"})
        )
    else:
        request.admission = replace(
            request.admission, backend_revision=REVISION.model_copy(update={"model": None})
        )
    monkeypatch.setattr(registry, "import_module", _forbidden_import)
    with pytest.raises((AdmissionDenied, ScopeViolation)):
        registry.build_channel_runtime(request)


def test_resource_authorization_rereads_the_provider_validated_manifest_each_time(registry):
    grants = SimpleNamespace(
        account_id="caller",
        platform="slack",
        channel_id="channel",
        config_digest="digest",
        project="project",
    )
    calls = []

    def read(tenant):
        calls.append(tenant)
        return (grants,) if len(calls) == 1 else ()

    def forbidden(request):
        raise AssertionError("resource authorization constructed a turn runtime")

    registry._BACKENDS[PROFILE] = HostedBackend(runtime=forbidden, authorizations=read)
    assert registry.authorized_channel_deployments(PROFILE, "tenant") == (grants,)
    assert registry.authorized_channel_deployments(PROFILE, "tenant") == ()
    assert calls == ["tenant", "tenant"]


def test_missing_resource_authorizer_refuses_without_runtime_or_legacy_fallback(registry):
    def forbidden(request):
        raise AssertionError("missing authorizer constructed a runtime")

    registry._BACKENDS[PROFILE] = HostedBackend(runtime=forbidden)
    with pytest.raises(UnsupportedCapability):
        registry.authorized_channel_deployments(PROFILE, "tenant")


_NATIVE_MODULES = frozenset(
    {
        "daimon.core.turn.openai_host",
        "daimon.core.turn.hosted_codec",
        "daimon.core.turn.hosted_io",
        "daimon.core.turn.openai_state",
        "daimon.core.turn.openai_deployment",
        "daimon.core.turn.gemini",
        "daimon.core.turn.gemini_prepare",
        "daimon.core.turn.gemini_state",
        "daimon.core.turn.gemini_deployment",
    }
)


def test_neutral_core_has_no_provider_only_import_edges():
    from daimon.core import channel_backend, mux_backend
    from daimon.core.turn import io, prepare

    modules = (channel_backend, mux_backend, io, prepare, runtimes)
    for module in modules:
        source = Path(module.__file__).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                targets = [imported, *(imported + "." + item.name for item in node.names)]
            elif isinstance(node, ast.Import):
                targets = [item.name for item in node.names]
            else:
                continue
            assert not any(target in _NATIVE_MODULES for target in targets), (
                module.__name__,
                node.lineno,
                targets,
            )


def test_cold_default_and_selected_refusal_work_without_provider_only_modules():
    code = rf"""
import importlib.abc
import json
import sys
from types import SimpleNamespace

blocked = {set(_NATIVE_MODULES)!r}
class NoNativeModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoNativeModules())
from daimon.core import channel_backend, mux_backend
from daimon.core.turn import io, prepare, runtimes
from daimon.core.turn.errors import AdmissionDenied
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef, Scope
assert runtimes.hosted_backend("anthropic.managed_agents") is None
assert not runtimes._BACKENDS
scope = Scope(tenant_id="tenant", account_id="account", principal_id="caller", authorization_id="authorized")
revision = ConfigRevision.create(ChannelRef(tenant_id="tenant", platform="slack", channel_id="channel"), 1,
    resolve_default(BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna")))
request = SimpleNamespace(admission=SimpleNamespace(backend_revision=revision, shared_owner=None),
    deps=SimpleNamespace(turn_path="mux", channel_backends=True), scope=scope,
    platform="slack", tenant_id="tenant", session_account_id="account", thread_id="thread")
try:
    runtimes.build_channel_runtime(request)
except AdmissionDenied as error:
    assert error.reason == "backend_unsupported"
else:
    raise AssertionError("missing provider ran or fell back")
assert not blocked.intersection(sys.modules)
print(json.dumps({{"default_discovery": 0, "selected_refused": True, "native_modules": 0}}))
"""
    assert json.loads(_cold(code)) == {
        "default_discovery": 0,
        "selected_refused": True,
        "native_modules": 0,
    }


def test_default_cli_copy_runs_without_provider_only_modules():
    code = rf"""
import asyncio
import importlib.abc
import json
import sys
import uuid
from types import SimpleNamespace
blocked = {set(_NATIVE_MODULES)!r}
class NoNativeModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoNativeModules())
from daimon.adapters.cli.commands import channels
from daimon.core.turn import runtimes
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef

def forbidden(name):
    raise AssertionError("default CLI imported an optional provider")
runtimes.import_module = forbidden
class Session:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
class Factory:
    def __call__(self, **kwargs):
        raise AssertionError("default CLI constructed a resource host")
tenant = uuid.UUID(int=4004)
channel = ChannelRef(tenant_id=str(tenant), platform="slack", channel_id="channel")
revision = ConfigRevision.create(channel, 1, resolve_default(BackendConfig()))
rt = SimpleNamespace(gemini_resource_hosts=None, resource_hosts=Factory(), sessionmaker=Session)
async def prove():
    queries = []
    for configured in (None, revision):
        async def read(session, ref):
            assert ref == channel
            queries.append(ref)
            return configured
        channels.current_backend = read
        result = await channels._copy_resource_host(rt, tenant_id=tenant, platform="slack", channel="channel")
        assert result is None
    assert len(queries) == 2
    assert not blocked.intersection(sys.modules)
    return {{"default_cli_cases": 2, "native_modules": 0}}
print(json.dumps(asyncio.run(prove())))
"""
    assert json.loads(_cold(code)) == {"default_cli_cases": 2, "native_modules": 0}


def test_openai_authorization_reads_only_the_selected_profile(registry, monkeypatch, tmp_path):
    from daimon.core.turn.openai_deployment import (
        OpenAIDeployment,
        authorized_deployments,
    )

    common = dict(
        tenant_id="tenant",
        platform="slack",
        channel_id="channel",
        account_id="account",
        config_digest="digest",
        project="project",
        agent_id="agent",
        api_key_env="OFFLINE_OPENAI_KEY",
    )
    persistent = OpenAIDeployment(profile=PROFILE, environment_id="environment", **common)
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps([persistent.model_dump(mode="json")]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    assert authorized_deployments("tenant") == (persistent,)
    assert registry.authorized_channel_deployments(PROFILE, "tenant") == (persistent,)
    with pytest.raises(UnsupportedCapability, match="host_deployment_authorization"):
        registry.authorized_channel_deployments("openai.conversation_only", "tenant")
    path.write_text(
        json.dumps(
            [
                persistent.model_dump(mode="json"),
                {"profile": "openai.conversation_only", "tenant_id": "tenant"},
            ]
        )
    )
    assert registry.authorized_channel_deployments(PROFILE, "tenant") == (persistent,)
    with pytest.raises(UnsupportedCapability, match="host_deployment_authorization"):
        registry.authorized_channel_deployments("openai.conversation_only", "tenant")

"""Installed OpenAI/Gemini distributions register their complete host edges.

These cases need the provider host modules and their runtime entry points, so
they live with the provider runtimes rather than the neutral registry tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from .test_provider_dispatch import PROFILE


@pytest.mark.parametrize("profile", [PROFILE, "gemini.inline_reuse"])
def test_installed_provider_registers_all_host_edges_without_credentials_or_clients(profile):
    code = rf"""
import json
import os
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from google.genai import Client

def no_client(*args, **kwargs):
    raise AssertionError("registration constructed a provider client")
for client_type in (AsyncAnthropic, AsyncOpenAI, Client):
    client_type.__init__ = no_client
from daimon.core import mux_backend
from daimon.core.turn import io, prepare, runtimes
profile = {profile!r}
original_get = os.environ.get
secret_names = {{"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "DAIMON_CHANNEL_OPENAI_KEY", "DAIMON_CHANNEL_GEMINI_KEY"}}
def no_secret(name, *args):
    if name in secret_names:
        raise AssertionError("registration read a credential")
    return original_get(name, *args)
os.environ.get = no_secret
assert runtimes.ensure_channel_runtime(profile)
assert profile in mux_backend._TURN_BACKENDS
assert profile in io._TURN_CODECS
assert profile in prepare._PROVIDER_PREPARATIONS
assert profile in runtimes._RUNTIME_FACTORIES
other = "gemini.inline_reuse" if profile == "openai.persistent_workspace" else "openai.persistent_workspace"
assert other not in mux_backend._TURN_BACKENDS
assert other not in io._TURN_CODECS
assert other not in prepare._PROVIDER_PREPARATIONS
assert runtimes.ensure_channel_runtime(profile)
print(json.dumps({{"selected_registered": True, "other_registered": False}}))
"""
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
    assert json.loads(result.stdout) == {"selected_registered": True, "other_registered": False}


@pytest.mark.parametrize("optimized", [False, True])
@pytest.mark.parametrize("profile", [PROFILE, "gemini.inline_reuse"])
def test_cold_explicit_runtime_reaches_actual_native_preparation(profile, optimized):
    code = rf"""
import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from sqlalchemy.ext.asyncio import async_sessionmaker
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from google.genai import Client

def no_client(*args, **kwargs):
    raise RuntimeError("cold registration constructed a native client")
for client_type in (AsyncAnthropic, AsyncOpenAI, Client):
    client_type.__init__ = no_client
from daimon.core import mux_backend
from daimon.core.turn import io, prepare, runtimes
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
sys.path.insert(0, os.path.join(os.getcwd(), "packages/core/tests"))
from turn.test_provider_dispatch import ACCOUNT, TENANT, admission, deps_for_pinned_mux
profile = {profile!r}
if profile in prepare._PROVIDER_PREPARATIONS:
    raise RuntimeError("test did not start with a cold native preparation edge")
class FactoryReached(Exception):
    pass
calls = []
reads = []
install_results = []
install = prepare.ensure_channel_runtime
def observed_install(*args, **kwargs):
    result = install(*args, **kwargs)
    install_results.append(result)
    return result
prepare.ensure_channel_runtime = observed_install
def factory(request):
    calls.append(request.admission.backend_revision.profile)
    raise FactoryReached()
def authorizations(tenant):
    reads.append(tenant)
    return ()
runtimes.register_channel_runtime(profile, factory, authorizations=authorizations)
original_get = os.environ.get
def no_secret(name, *args):
    if name in {{"DAIMON_TURN__PROVIDER_RUNTIME_FILE", "DAIMON_CHANNEL_OPENAI_KEY", "DAIMON_CHANNEL_GEMINI_KEY"}}:
        raise RuntimeError("cold installation inspected deployment or credentials")
    return original_get(name, *args)
os.environ.get = no_secret
async def prove():
    selected = admission()
    config = ConfigRevision.create(selected.backend_revision.channel, 1,
        resolve_default(BackendConfig(backend="openai" if profile == "openai.persistent_workspace" else "gemini",
            profile=profile, model="gpt-6-luna" if profile == "openai.persistent_workspace" else "gemini-3.8-flash")))
    selected = replace(selected, backend_revision=config, backend_revision_origin="channel")
    deps = replace(deps_for_pinned_mux(None, async_sessionmaker()), channel_backends=True,
        turn_runtime_factory=runtimes.build_channel_runtime)
    try:
        await prepare.bind_session_impl(deps, selected, tenant_id=TENANT, platform="slack",
            external_user_id="caller", thread_id="thread", session_account_id=ACCOUNT,
            reuse_existing=True, deadline=datetime.now(UTC)+timedelta(seconds=15))
    except FactoryReached:
        pass
    else:
        raise RuntimeError("actual native preparer did not reach the explicit constructor")
    if calls != [profile] or install_results != [True]:
        raise RuntimeError("explicit constructor or completed installation differs")
    if any(profile not in owner for owner in (mux_backend._TURN_BACKENDS, io._TURN_CODECS, prepare._PROVIDER_PREPARATIONS)):
        raise RuntimeError("provider host bundle remained incomplete")
    record = runtimes._RUNTIME_FACTORIES[profile]
    if record.factory is not factory or record.authorizations is not authorizations:
        raise RuntimeError("provider registrar replaced an explicit callback")
    if runtimes.authorized_channel_deployments(profile, "tenant") != () or reads != ["tenant"]:
        raise RuntimeError("explicit resource authorization was not retained")
    return {{"constructor_calls": len(calls), "host_bundle_complete": True, "explicit_reader_retained": True}}
print(json.dumps(asyncio.run(prove())))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH"
        and not key.startswith(("OPENAI_", "ANTHROPIC_", "GOOGLE_", "GEMINI_"))
    }
    command = [sys.executable, *(["-O"] if optimized else []), "-c", code]
    result = subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=40, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "constructor_calls": 1,
        "host_bundle_complete": True,
        "explicit_reader_retained": True,
    }

"""Provider-host cases of test_openai_resource_routing: they need the OpenAI/Gemini host modules,
so they land with the provider runtimes.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from daimon.adapters.cli.commands.agents import agents_get
from daimon.adapters.cli.commands.channels import channels_rule_set
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.neutral_catalog import AgentRevision
from mux.contracts.config import BackendConfig
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import build_cli_runtime
from .test_openai_resource_routing import ACTORS as ACTORS
from .test_openai_resource_routing import CHANNEL as CHANNEL
from .test_openai_resource_routing import DEPLOYMENT_FILE as DEPLOYMENT_FILE
from .test_openai_resource_routing import GUILD as GUILD
from .test_openai_resource_routing import OPENAI as OPENAI
from .test_openai_resource_routing import PROJECT as PROJECT
from .test_openai_resource_routing import _agent as _agent
from .test_openai_resource_routing import _anthropic as _anthropic
from .test_openai_resource_routing import _channel_rt as _channel_rt
from .test_openai_resource_routing import _channel_settings as _channel_settings
from .test_openai_resource_routing import _cli_tenant as _cli_tenant
from .test_openai_resource_routing import _configure as _configure
from .test_openai_resource_routing import _console as _console
from .test_openai_resource_routing import _copy as _copy
from .test_openai_resource_routing import _deployment_account as _deployment_account
from .test_openai_resource_routing import _Factory as _Factory
from .test_openai_resource_routing import _get_manifest as _get_manifest
from .test_openai_resource_routing import _GetSettings as _GetSettings
from .test_openai_resource_routing import _isolatable as _isolatable
from .test_openai_resource_routing import _isolated as _isolated
from .test_openai_resource_routing import _ma as _ma
from .test_openai_resource_routing import _manifest as _manifest
from .test_openai_resource_routing import _no_ambient_manifest as _no_ambient_manifest
from .test_openai_resource_routing import _opened as _opened
from .test_openai_resource_routing import _out as _out
from .test_openai_resource_routing import _seed as _seed
from .test_openai_resource_routing import _Stop as _Stop
from .test_openai_resource_routing import _with as _with
from .test_openai_resource_routing import seen as seen

PRINCIPAL = "cli:testuser"

AUTHORIZATION = "cli-deployment-operator"


def _no_egress(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"native I/O for an unchecked host: {request.url}")


def _scope(tenant_id: uuid.UUID, account_id: uuid.UUID, principal: str = PRINCIPAL) -> Scope:
    return Scope(
        tenant_id=str(tenant_id),
        account_id=str(account_id),
        principal_id=principal,
        authorization_id=AUTHORIZATION,
    )


def _driver(scope: Scope, workspace: str) -> Any:
    from mux.drivers.openai import OpenAIDriver
    from mux.drivers.openai.transport import SDKTransport
    from mux.drivers.openai.turn import MemoryRecoveryJournal
    from mux.drivers.openai.usage import MemoryUsageRevisions
    from mux.testing.isolated_sdk import build_async_openai

    return OpenAIDriver(
        SDKTransport(build_async_openai(transport=httpx.MockTransport(_no_egress))),
        account_scope_id=workspace,
        authorization=lambda authorized, _kind, _id: authorized == scope,
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
    )


def _real_host(
    sessions: async_sessionmaker[AsyncSession], scope: Scope, workspace: str = PROJECT
) -> NeutralResourceHost:
    """A real neutral resource host whose provider transport refuses any request."""
    return NeutralResourceHost(
        _driver(scope, workspace),
        sessions,
        scope=scope,
        account_scope_id=workspace,
        operation_key="test-operation",
    )


MISMATCHES = ("foreign_tenant", "foreign_account", "other_workspace", "other_principal")


def _mismatched(
    sessions: async_sessionmaker[AsyncSession],
    kind: str,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> NeutralResourceHost:
    if kind == "foreign_tenant":
        return _real_host(sessions, _scope(uuid.uuid4(), account_id))
    if kind == "foreign_account":
        return _real_host(sessions, _scope(tenant_id, uuid.uuid4()))
    if kind == "other_workspace":
        return _real_host(sessions, _scope(tenant_id, account_id), workspace="other-project")
    return _real_host(sessions, _scope(tenant_id, account_id, principal="cli:someone-else"))


async def test_an_authorized_openai_channel_copies_through_the_operator_host(
    seen: list[object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _isolatable(db_session_factory)
    revision = await _configure(db_session_factory, tenant_id, OPENAI)
    account = uuid.uuid4()
    _manifest(
        tmp_path,
        monkeypatch,
        tenant_id=tenant_id,
        account_id=account,
        platform="discord",
        channel_id=CHANNEL,
        digest=revision.digest,
    )
    host = _real_host(db_session_factory, _scope(tenant_id, account))
    factory = _Factory(host)
    with pytest.raises(_Stop):
        await _copy(_channel_rt(db_session_factory, tenant_id, factory))
    assert seen == [host]
    assert [account] == ACTORS, "core binds the host to the authorized actor account"
    (call,) = factory.calls
    assert (call["tenant_id"], call["account_id"]) == (tenant_id, account)
    assert (call["principal_id"], call["authorization_id"]) == (PRINCIPAL, AUTHORIZATION)
    assert call["operation_key"].startswith("cli-channel-copy:")


@pytest.mark.parametrize("kind", MISMATCHES)
async def test_a_copy_through_a_mismatched_host_is_refused_before_any_change(
    kind: str,
    seen: list[object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _isolatable(db_session_factory)
    revision = await _configure(db_session_factory, tenant_id, OPENAI)
    account = uuid.uuid4()
    _manifest(
        tmp_path,
        monkeypatch,
        tenant_id=tenant_id,
        account_id=account,
        platform="discord",
        channel_id=CHANNEL,
        digest=revision.digest,
    )
    async with db_session_factory() as session:
        before = await load_access_policy(session, tenant_id=tenant_id)
    host = _mismatched(db_session_factory, kind, tenant_id=tenant_id, account_id=account)
    console = _console()
    with pytest.raises(typer.Exit):
        await channels_rule_set(
            rt=_channel_rt(db_session_factory, tenant_id, _Factory(host)),
            console=console,
            platform="discord",
            workspace_id=GUILD,
            channel_id=CHANNEL,
            readers="own",
            copy_from="shared",
            discord_transport=None,
        )
    assert "different tenant, account or workspace" in _out(console)
    assert seen == [], "the core copy edge is never reached"
    async with db_session_factory() as session:
        assert await load_access_policy(session, tenant_id=tenant_id) == before


@pytest.mark.parametrize("manifest", ["absent", "stale_revision", "other_channel"])
async def test_an_openai_channel_without_an_authorized_deployment_keeps_its_path(
    manifest: str,
    seen: list[object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _isolatable(db_session_factory)
    first = await _configure(db_session_factory, tenant_id, OPENAI)
    if manifest == "stale_revision":
        # The manifest authorizes an earlier revision; the channel has moved on.
        await _configure(
            db_session_factory,
            tenant_id,
            BackendConfig(
                backend="openai",
                profile="openai.persistent_workspace",
                model="gpt-6-luna",
                thread_mode="shared",
            ),
        )
    if manifest != "absent":
        _manifest(
            tmp_path,
            monkeypatch,
            tenant_id=tenant_id,
            account_id=uuid.uuid4(),
            platform="discord",
            channel_id=CHANNEL if manifest == "stale_revision" else "900000000000000009",
            digest=first.digest,
        )
    factory = _Factory(object())
    with pytest.raises(_Stop):
        await _copy(_channel_rt(db_session_factory, tenant_id, factory))
    assert seen == [None]
    assert factory.calls == []


class _Host(NeutralResourceHost):
    """A real, correctly scoped host whose native readback is recorded."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], scope: Scope, row: AgentRevision
    ) -> None:
        super().__init__(
            _driver(scope, PROJECT),
            sessions,
            scope=scope,
            account_scope_id=PROJECT,
            operation_key="test-operation",
        )
        self.row = row
        self.read_ids: list[str] = []

    async def read(self, catalog_id: str) -> AgentRevision:
        self.read_ids.append(catalog_id)
        return self.row


async def test_an_unconfigured_tenant_falls_back_without_opening_the_catalog(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The capability is installed, but nothing authorizes this tenant natively."""
    opened = _opened(monkeypatch)
    factory = _Factory(object())
    rt = _with(
        build_cli_runtime(
            db_session_factory, anthropic=_anthropic("qa-agent"), settings=_GetSettings()
        ),
        factory,
    )
    console = _console()
    await agents_get(rt=rt, console=console, name="qa-agent", as_json=False)
    assert opened == [] and factory.calls == []
    assert "agent_anthropic" in _out(console)


async def test_get_reads_an_authorized_openai_agent_back_through_the_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account = await _deployment_account(db_session_factory)
    row = await _seed(db_session_factory, account, "qa-agent")
    _get_manifest(tmp_path, monkeypatch, account)
    opened = _opened(monkeypatch)
    host = _Host(db_session_factory, _scope(_cli_tenant(), account), row)
    factory = _Factory(host)

    def no_anthropic(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"an OpenAI agent was looked up on Anthropic: {request.url}")

    rt = _with(
        build_cli_runtime(
            db_session_factory, anthropic=_isolated(no_anthropic), settings=_GetSettings()
        ),
        factory,
    )
    console = _console()
    await agents_get(rt=rt, console=console, name="qa-agent", as_json=True)
    assert opened == [PROJECT]
    assert host.read_ids == ["qa-agent"], "the provider's agent is read back, not the row alone"
    out = _out(console)
    assert "agent_native" in out and "qa-original" in out
    assert "gpt-6-luna" in out and "skill_own" in out
    (call,) = factory.calls
    assert call["account_id"] == account
    assert call["operation_key"].startswith("cli-agent-get:")


async def test_an_authorized_tenant_without_that_agent_asks_for_no_host_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account = await _deployment_account(db_session_factory)
    _get_manifest(tmp_path, monkeypatch, account)
    opened = _opened(monkeypatch)
    factory = _Factory(object())
    rt = _with(
        build_cli_runtime(
            db_session_factory, anthropic=_anthropic("qa-agent"), settings=_GetSettings()
        ),
        factory,
    )
    console = _console()
    await agents_get(rt=rt, console=console, name="qa-agent", as_json=False)
    assert opened == [PROJECT] and factory.calls == []
    assert "agent_anthropic" in _out(console)


@pytest.mark.parametrize("kind", MISMATCHES)
async def test_get_through_a_mismatched_host_refuses_without_reading_or_falling_back(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.adapters.cli.runtime import ResourceHostMismatch

    account = await _deployment_account(db_session_factory)
    await _seed(db_session_factory, account, "qa-agent")
    _get_manifest(tmp_path, monkeypatch, account)
    host = _mismatched(db_session_factory, kind, tenant_id=_cli_tenant(), account_id=account)

    def no_anthropic(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"fell back to Anthropic after a mismatched host: {request.url}")

    rt = _with(
        build_cli_runtime(
            db_session_factory, anthropic=_isolated(no_anthropic), settings=_GetSettings()
        ),
        _Factory(host),
    )
    console = _console()
    with pytest.raises(ResourceHostMismatch):
        await agents_get(rt=rt, console=console, name="qa-agent", as_json=True)
    assert "qa-original" not in _out(console)

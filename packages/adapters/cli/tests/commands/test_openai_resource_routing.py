"""OpenAI agents and channel copies through the operator's resource-host capability.

Routing only: which path a command takes, read fresh, and when a host is
requested. A host is requested only under the capability and for a tenant or
channel the operator's deployment manifest authorizes; everything else keeps
the existing path. The fork, CAS and native readback themselves are the core
host's (packages/core/tests/test_neutral_resource_host.py). Every SDK client
here comes from the isolated test factory.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import typer
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands import channels as channels_mod
from daimon.adapters.cli.commands.agents import agents_get
from daimon.adapters.cli.commands.channels import channels_rule_set
from daimon.adapters.cli.runtime import CliRuntime, ResourceHostFactory
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores import neutral_catalog
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.neutral_catalog import AgentRevision, PostgresNeutralCatalog
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.contracts.config import BackendConfig, ConfigRevision
from mux.contracts.ids import ModelRef, ResourceRef, Revision, Scope, SkillRef
from mux.contracts.resources import Agent, AgentSpec
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    MARouter,
    list_response,
    make_fake_ma_handler,
)
from mux.testing.isolated_sdk import AmbientSDKConfigurationError, build_async_anthropic
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import FakeCliSettings, build_cli_runtime

GUILD = "900000000000000001"
CHANNEL = "900000000000000002"
PROJECT = "project"
OPENAI = BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna")
GEMINI = BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")
ANTHROPIC = BackendConfig()
DEPLOYMENT_FILE = "DAIMON_TURN__PROVIDER_RUNTIME_FILE"
# The actor account each stopped `set_channel_rule` call received (see `seen`).
ACTORS: list[object] = []


def _out(console: Console) -> str:
    return cast(StringIO, console.file).getvalue()


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=200)


def _isolated(handler: Callable[[httpx.Request], httpx.Response]) -> AsyncAnthropic:
    """A test Anthropic client that takes nothing from the environment."""
    return build_async_anthropic(transport=httpx.MockTransport(handler))


def _ma(tenant_id: uuid.UUID, *names: str) -> AsyncAnthropic:
    state = FakeMAState()
    for name in names:
        agent = ma_agent(id=f"agent_{name}", name=name, tenant_id=tenant_id)
        state.agents[agent.id] = agent.model_dump(mode="json")
    return _isolated(make_fake_ma_handler(state))


def _channel_settings() -> SimpleNamespace:
    return SimpleNamespace(
        cli=FakeCliSettings.cli,
        discord=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "t")),
        mcp=SimpleNamespace(public_url=None),
        crypto=SimpleNamespace(keys=()),
    )


class _GetSettings:
    cli = FakeCliSettings.cli
    mcp = SimpleNamespace(public_url=None)


class _Factory:
    """Records every host request; the host it returns is a stand-in."""

    account_scope_id = PROJECT

    def __init__(self, host: object) -> None:
        self.host = host
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.host


class _Stop(Exception):
    pass


def _with(rt: CliRuntime, factory: _Factory | None) -> CliRuntime:
    return dataclasses.replace(rt, resource_hosts=cast(ResourceHostFactory | None, factory))


def _manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    channel_id: str,
    digest: str,
) -> None:
    """The operator's deployment manifest (#767) authorizing one channel revision."""
    row = {
        "profile": "openai.persistent_workspace",
        "tenant_id": str(tenant_id),
        "platform": platform,
        "channel_id": channel_id,
        "account_id": str(account_id),
        "config_digest": digest,
        "project": PROJECT,
        "agent_id": "agent_deployed",
        "environment_id": "env_deployed",
        "api_key_env": "QA_UNSET_DEPLOYMENT_KEY",
    }
    path = tmp_path / "deployments.json"
    path.write_text(json.dumps([row]))
    monkeypatch.setenv(DEPLOYMENT_FILE, str(path))


@pytest.fixture(autouse=True)
def _no_ambient_manifest(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.delenv(DEPLOYMENT_FILE, raising=False)


# channels rule set --copy-from


async def _isolatable(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A Discord tenant whose channel answers with a shared agent, so `own` needs a copy."""
    async with sessions.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=GUILD)
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=CHANNEL),
            tenant_id=tenant.id,
            agent_name="shared",
            mode="agent",
        )
    return tenant.id


async def _configure(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, config: BackendConfig
) -> ConfigRevision:
    async with sessions.begin() as session:
        return await set_channel_backend(
            session, channel_ref(tenant_id, "discord", CHANNEL), config
        )


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """What `set_channel_rule` was handed as its resource host; it stops there."""
    hosts: list[object] = []
    ACTORS.clear()

    async def record(
        *_args: object,
        resource_host: object = None,
        actor_account_id: object = None,
        **_kwargs: object,
    ) -> object:
        hosts.append(resource_host)
        ACTORS.append(actor_account_id)
        raise _Stop

    monkeypatch.setattr(channels_mod, "set_channel_rule", record)
    return hosts


async def _copy(rt: CliRuntime) -> Console:
    console = _console()
    await channels_rule_set(
        rt=rt,
        console=console,
        platform="discord",
        workspace_id=GUILD,
        channel_id=CHANNEL,
        readers="own",
        copy_from="shared",
        discord_transport=None,
    )
    return console


def _channel_rt(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, factory: _Factory | None
) -> CliRuntime:
    rt = build_cli_runtime(
        sessions, anthropic=_ma(tenant_id, "shared"), settings=_channel_settings()
    )
    return _with(rt, factory)


@pytest.mark.parametrize("config", [OPENAI, GEMINI, ANTHROPIC, None])
async def test_without_the_capability_every_channel_copies_on_the_existing_path(
    config: BackendConfig | None,
    seen: list[object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _isolatable(db_session_factory)
    if config is not None:
        revision = await _configure(db_session_factory, tenant_id, config)
        _manifest(
            tmp_path,
            monkeypatch,
            tenant_id=tenant_id,
            account_id=uuid.uuid4(),
            platform="discord",
            channel_id=CHANNEL,
            digest=revision.digest,
        )
    with pytest.raises(_Stop):
        await _copy(_channel_rt(db_session_factory, tenant_id, None))
    assert seen == [None]


@pytest.mark.parametrize("config", [ANTHROPIC, None])
async def test_an_anthropic_or_unconfigured_channel_keeps_its_path_and_asks_for_no_host(
    config: BackendConfig | None,
    seen: list[object],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _isolatable(db_session_factory)
    if config is not None:
        await _configure(db_session_factory, tenant_id, config)
    factory = _Factory(object())
    with pytest.raises(_Stop):
        await _copy(_channel_rt(db_session_factory, tenant_id, factory))
    assert seen == [None]
    assert factory.calls == []


async def test_a_gemini_channel_copy_is_refused_and_nothing_changes(
    seen: list[object], db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _isolatable(db_session_factory)
    await _configure(db_session_factory, tenant_id, GEMINI)
    async with db_session_factory() as session:
        before = await load_access_policy(session, tenant_id=tenant_id)
    factory = _Factory(object())
    console = _console()
    with pytest.raises(typer.Exit):
        await channels_rule_set(
            rt=_channel_rt(db_session_factory, tenant_id, factory),
            console=console,
            platform="discord",
            workspace_id=GUILD,
            channel_id=CHANNEL,
            readers="own",
            copy_from="shared",
            discord_transport=None,
        )
    assert "can't make its own copy" in _out(console)
    assert factory.calls == [] and seen == []
    async with db_session_factory() as session:
        assert await load_access_policy(session, tenant_id=tenant_id) == before


# agents get, over the real neutral catalog


def _cli_tenant() -> uuid.UUID:
    """The cli:local tenant the conftest seeds; the command adds its principal itself."""
    return derive_tenant_uuid(platform="cli", workspace_id="local")


async def _deployment_account(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessions.begin() as session:
        tenant = await get_tenant(session, _cli_tenant())
        assert tenant is not None
        return (await make_account(session, tenant=tenant)).id


def _agent(account_id: uuid.UUID, name: str) -> Agent:
    return Agent(
        ref=ResourceRef(
            id="agent_native",
            kind="agent",
            provider="openai",
            account_scope_id=PROJECT,
            tenant_id=str(_cli_tenant()),
            account_id=str(account_id),
        ),
        revision=Revision(local=0),
        created_at=datetime(2026, 10, 10, tzinfo=UTC),
        spec=AgentSpec(
            name=name,
            description="qa-original",
            model=ModelRef(provider="openai", id="gpt-6-luna"),
            skills=(SkillRef(id="skill_own", version="3"),),
        ),
    )


async def _seed(
    sessions: async_sessionmaker[AsyncSession], account_id: uuid.UUID, name: str
) -> AgentRevision:
    scope = Scope(
        tenant_id=str(_cli_tenant()),
        account_id=str(account_id),
        principal_id="cli:testuser",
        authorization_id="cli-deployment-operator",
    )
    catalog = PostgresNeutralCatalog(sessions, provider="openai", account_scope_id=PROJECT)
    return await catalog.put_agent(scope, name, _agent(account_id, name), expected_revision=0)


def _opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every catalog the command opens, still the real store underneath."""
    opened: list[str] = []
    real = PostgresNeutralCatalog

    def counting(*args: Any, **kwargs: Any) -> PostgresNeutralCatalog:
        opened.append(kwargs["account_scope_id"])
        return real(*args, **kwargs)

    monkeypatch.setattr(neutral_catalog, "PostgresNeutralCatalog", counting)
    return opened


def _anthropic(name: str) -> AsyncAnthropic:
    agent = ma_agent(id="agent_anthropic", name=name, tenant_id=_cli_tenant())
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent.model_dump(mode="json")]))
    return _isolated(router.dispatch)


def _get_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: uuid.UUID) -> None:
    _manifest(
        tmp_path,
        monkeypatch,
        tenant_id=_cli_tenant(),
        account_id=account,
        platform="cli",
        channel_id="local",
        digest="sha256:deployed",
    )


async def test_without_the_capability_get_opens_no_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account = await _deployment_account(db_session_factory)
    await _seed(db_session_factory, account, "qa-agent")
    _get_manifest(tmp_path, monkeypatch, account)
    opened = _opened(monkeypatch)
    rt = build_cli_runtime(
        db_session_factory, anthropic=_anthropic("qa-agent"), settings=_GetSettings()
    )
    console = _console()
    await agents_get(rt=rt, console=console, name="qa-agent", as_json=False)
    assert opened == []
    assert "agent_anthropic" in _out(console)


# The test clients themselves: ambient SDK settings refused by name.


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_WEBHOOK_SIGNING_KEY", "ANTHROPIC_BASE_URL"],
)
@pytest.mark.parametrize("value", ["", "ambient"])
def test_the_test_clients_refuse_ambient_settings_by_name(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_egress(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"egress attempted: {request.url}")

    monkeypatch.setenv(name, value)
    with pytest.raises(AmbientSDKConfigurationError, match=name):
        _isolated(no_egress)
    with pytest.raises(AmbientSDKConfigurationError, match=name):
        _ma(uuid.uuid4(), "shared")

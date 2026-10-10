"""Per-channel backend configuration: off by default, checked at admission when on."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from daimon.core.channel_backend import (
    BackendUnsupported,
    channel_ref,
    check_backend,
    clear_channel_backend,
    current_backend,
    set_channel_backend,
)
from daimon.core.config import McpSettings, TurnSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import TenantRow
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.testing.factories import make_ledger_entry, make_tenant, make_tenant_config
from daimon.testing.ma import build_fake_anthropic, resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.config import (
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    resolve_default,
)
from mux.contracts.ids import ChannelRef
from mux.errors import InvalidConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 10, 9, tzinfo=UTC)

# The resolved configuration of a channel nobody configured. If this changes,
# every legacy channel's backend changed with it.
_DEFAULT_DIGEST = "663d9f10dd4cb4f1a954eeedbaf69cf02fe55b2adf4707511d7176d0117fd221"

_CHANNEL = ChannelRef(tenant_id=str(uuid.uuid4()), platform="discord", channel_id="chan-1")


def test_an_unconfigured_channel_resolves_to_anthropic_managed_agents_unchanged() -> None:
    resolved = resolve_default(None)
    assert resolved.model_dump(mode="json") == {
        "backend": "anthropic",
        "profile": "anthropic.managed_agents",
        "model": None,
        "requires": {},
        "thread_mode": "per_caller",
    }
    assert resolved.content_digest() == _DEFAULT_DIGEST
    assert resolve_default(BackendConfig()).content_digest() == _DEFAULT_DIGEST
    check_backend(ConfigRevision.create(_CHANNEL, 1, resolved))


def test_shared_threads_on_anthropic_are_runnable() -> None:
    check_backend(_revision(BackendConfig(thread_mode="shared")))


def test_turn_settings_leave_channel_backends_off() -> None:
    assert TurnSettings().channel_backends is False


def _revision(config: BackendConfig) -> ConfigRevision:
    return ConfigRevision.create(_CHANNEL, 1, resolve_default(config))


@pytest.mark.parametrize(
    "config",
    [
        # The profile cannot meet a requirement.
        BackendConfig(
            backend="openai",
            profile="openai.conversation_only",
            model="gpt-5",
            requires={"thread_workspace_persistence": CapabilityRequirement(level="required")},
        ),
        # Admitted by its profile, but not runnable in this release.
        BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-5"),
        BackendConfig(model="claude-opus-5-5"),
    ],
)
def test_a_configuration_this_release_cannot_honour_is_refused(config: BackendConfig) -> None:
    with pytest.raises(BackendUnsupported):
        check_backend(_revision(config))
    denied = AdmissionDenied(reason="backend_unsupported")
    assert termination_reason(denied) is TerminationReason.ADMISSION_DENIED
    nouns = RefusalNouns(scope="server", admin="a server admin", billing="/billing")
    assert "backend" in admission_refusal_text("backend_unsupported", nouns)


async def test_set_and_clear_write_immutable_revisions(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    channel = channel_ref(tenant.id, "discord", "chan-1")
    assert await current_backend(db_session, channel) is None

    openai = BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-5")
    first = await set_channel_backend(db_session, channel, openai)
    assert (first.local, first.profile, first.model) == (1, "openai.persistent_workspace", "gpt-5")
    assert await set_channel_backend(db_session, channel, openai) == first
    cleared = await clear_channel_backend(db_session, channel)
    assert (cleared.local, cleared.digest) == (2, _DEFAULT_DIGEST)
    assert await current_backend(db_session, channel) == cleared

    with pytest.raises(InvalidConfig):
        await set_channel_backend(
            db_session, channel, BackendConfig(backend="openai", profile="anthropic.managed_agents")
        )
    with pytest.raises(InvalidConfig):
        await set_channel_backend(
            db_session, channel, BackendConfig(backend="openai", profile="openai.nope", model="m")
        )


# Admission


async def _tenant(session: AsyncSession) -> TenantRow:
    tenant = await make_tenant(session)
    await make_tenant_config(
        session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("10"))
    await session.commit()
    return tenant


def _deps(
    tenant: TenantRow,
    sessionmaker: async_sessionmaker[AsyncSession],
    defaults_root: Path,
    calls: list[httpx.Request],
    *,
    channel_backends: bool,
) -> TurnDeps:
    router = resolved_agent_env_router(
        ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )

    def dispatch(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return router.dispatch(request)

    return TurnDeps(
        anthropic=build_fake_anthropic(dispatch),
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=defaults_root,
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1.0"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        channel_backends=channel_backends,
    )


async def _admit(deps: TurnDeps, tenant: TenantRow, channel_id: str = "chan-1") -> Admission:
    return await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        channel_id=channel_id,
        now=_NOW,
    )


_UNRUNNABLE = BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-5")


async def test_with_the_flag_off_admission_never_reads_the_configuration(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _tenant(db_session)
    await set_channel_backend(db_session, channel_ref(tenant.id, "discord", "chan-1"), _UNRUNNABLE)
    await db_session.commit()
    off = _deps(tenant, db_session_factory, tmp_path, [], channel_backends=False)

    admission = await _admit(off, tenant)

    assert admission.backend_revision is None
    assert admission.backend is None


async def test_with_the_flag_on_an_unconfigured_channel_admits_as_before(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _tenant(db_session)
    off_calls: list[httpx.Request] = []
    on_calls: list[httpx.Request] = []
    off = await _admit(
        _deps(tenant, db_session_factory, tmp_path, off_calls, channel_backends=False), tenant
    )
    on = await _admit(
        _deps(tenant, db_session_factory, tmp_path, on_calls, channel_backends=True), tenant
    )

    assert on == off
    assert on.backend_revision is None
    assert [(r.method, r.url.path) for r in on_calls] == [(r.method, r.url.path) for r in off_calls]


async def test_with_the_flag_on_a_runnable_configuration_is_admitted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _tenant(db_session)
    await clear_channel_backend(db_session, channel_ref(tenant.id, "discord", "chan-1"))
    await db_session.commit()
    on = _deps(tenant, db_session_factory, tmp_path, [], channel_backends=True)

    admission = await _admit(on, tenant)

    assert admission.backend_revision is not None
    assert admission.backend_revision.digest == _DEFAULT_DIGEST
    assert admission.backend is not None


async def test_with_the_flag_on_an_unrunnable_configuration_is_refused_before_any_ma_call(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _tenant(db_session)
    await set_channel_backend(db_session, channel_ref(tenant.id, "discord", "chan-1"), _UNRUNNABLE)
    await db_session.commit()
    calls: list[httpx.Request] = []
    on = _deps(tenant, db_session_factory, tmp_path, calls, channel_backends=True)

    with pytest.raises(AdmissionDenied) as raised:
        await _admit(on, tenant)

    assert raised.value.reason == "backend_unsupported"
    assert calls == []
    # Another channel of the same tenant is not affected.
    assert (await _admit(replace(on), tenant, "chan-2")).backend_revision is None

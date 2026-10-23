"""Per-channel backend configuration: off by default, checked at admission when on."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_backend import (
    BackendUnsupported,
    channel_ref,
    check_backend,
    clear_channel_backend,
    current_backend,
    set_channel_backend,
)
from daimon.core.config import McpSettings, TurnSettings, load_turn_settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import TenantRow
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.factories import make_ledger_entry, make_tenant, make_tenant_config
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.config import (
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    resolve_default,
)
from mux.contracts.ids import ChannelRef
from mux.errors import InvalidConfig
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

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


def test_turn_settings_turn_channel_backends_on_by_default() -> None:
    assert TurnSettings().channel_backends is True
    assert TurnSettings(channel_backends=False).channel_backends is False, "the operator stop"


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
    assert "model provider" in admission_refusal_text("backend_unsupported", nouns)


@pytest.mark.parametrize("capability", ["steer", "native_event_replay", "environments_fork"])
def test_a_capability_the_default_driver_refuses_is_refused_at_admission(capability: str) -> None:
    """The Anthropic profile no longer claims what its driver cannot do."""
    requirement = {capability: CapabilityRequirement(level="required")}
    with pytest.raises(BackendUnsupported, match=capability):
        check_backend(_revision(BackendConfig(requires=requirement)))  # type: ignore[arg-type]


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


_ARGON = BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-4-argon")


def _pricing_clock(monkeypatch: pytest.MonkeyPatch, day: datetime) -> None:
    from daimon.core import pricing

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # pyright: ignore[reportIncompatibleMethodOverride, reportMissingParameterType]
            return day

    monkeypatch.setattr(pricing, "datetime", Clock)


def test_argon_is_admitted_for_channels_and_workers_only_inside_its_price_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pricing_clock(monkeypatch, datetime(2026, 11, 30, tzinfo=UTC))
    check_backend(_revision(_ARGON))
    # A fleet worker may run Argon (the host keeps it on the bounded envelope).
    check_backend(_revision(_ARGON), worker=True)
    _pricing_clock(monkeypatch, datetime(2026, 12, 1, tzinfo=UTC))
    for worker in (False, True):
        with pytest.raises(
            BackendUnsupported, match="gemini-4-argon's price window ended on 2026-11-30"
        ):
            check_backend(_revision(_ARGON), worker=worker)
    # The Flash default and the cheap worker model are not tariff-gated.
    flash = BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")
    check_backend(_revision(flash))
    check_backend(
        _revision(flash.model_copy(update={"model": "gemini-3.5-flash-lite"})), worker=True
    )


async def test_argon_cannot_be_configured_after_its_price_window(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session)
    channel = channel_ref(tenant.id, "discord", "chan-argon")
    _pricing_clock(monkeypatch, datetime(2026, 12, 1, tzinfo=UTC))
    with pytest.raises(BackendUnsupported, match="price window ended on 2026-11-30"):
        await set_channel_backend(db_session, channel, _ARGON)
    assert await current_backend(db_session, channel) is None
    _pricing_clock(monkeypatch, datetime(2026, 11, 30, tzinfo=UTC))
    written = await set_channel_backend(db_session, channel, _ARGON)
    assert (written.local, written.model) == (1, "gemini-4-argon")


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
        requester_org=RequesterOrg.HOST,
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
    # An explicit pin to the default configuration. A cleared channel is a
    # different thing on the mux path: see the next test.
    await set_channel_backend(
        db_session, channel_ref(tenant.id, "discord", "chan-1"), BackendConfig()
    )
    await db_session.commit()
    on = _deps(tenant, db_session_factory, tmp_path, [], channel_backends=True)

    admission = await _admit(on, tenant)

    assert admission.backend_revision is not None
    assert admission.backend_revision.digest == _DEFAULT_DIGEST
    assert admission.backend is not None


async def test_with_the_flag_on_a_cleared_pin_is_admitted_as_unconfigured_only_on_mux(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Clearing a channel's pin (#1323) returns it to no configuration on the mux path.

    Backend selection (`TurnDeps.backend_selection_enabled`, mux plus the flag)
    reads a `channel_clear` revision as "no pin" (`turn/admission.py`
    `config_origin`), so admission carries no revision. The legacy path has no
    selection and still admits the cleared revision, which is the default.
    """
    tenant = await _tenant(db_session)
    await set_channel_backend(db_session, channel_ref(tenant.id, "discord", "chan-1"), _UNRUNNABLE)
    await clear_channel_backend(db_session, channel_ref(tenant.id, "discord", "chan-1"))
    await db_session.commit()
    on = _deps(tenant, db_session_factory, tmp_path, [], channel_backends=True)

    admission = await _admit(on, tenant)

    if (on.turn_path or load_turn_settings().path) == "mux":
        assert admission.backend_revision is None
        assert admission.backend is None
    else:
        assert admission.backend_revision is not None
        assert admission.backend_revision.digest == _DEFAULT_DIGEST
        assert admission.backend is not None


@pytest.mark.parametrize("provider_enabled", [True, False])
async def test_with_the_flag_on_an_unrunnable_configuration_is_refused_before_any_ma_call(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    provider_enabled: bool,
) -> None:
    tenant = await _tenant(db_session)
    if provider_enabled:
        await set_access_policy(
            db_session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(enabled_backends=("anthropic", "openai")),
        )
    await set_channel_backend(db_session, channel_ref(tenant.id, "discord", "chan-1"), _UNRUNNABLE)
    await db_session.commit()
    calls: list[httpx.Request] = []
    on = _deps(tenant, db_session_factory, tmp_path, calls, channel_backends=True)

    with pytest.raises(AdmissionDenied) as raised:
        await _admit(on, tenant)

    assert raised.value.reason == "backend_unsupported"
    # The refusal names the real cause (#1319). The workspace's provider
    # allowlist is read only when backend selection is on (mux plus the flag),
    # and then it comes before the pin; otherwise only the pin is in play.
    selection = (on.turn_path or load_turn_settings().path) == "mux"
    expected = "provider_not_allowed" if selection and not provider_enabled else "channel_pin"
    assert raised.value.cause == expected
    assert raised.value.provider == _UNRUNNABLE.backend
    assert calls == []
    # Another channel of the same tenant is not affected.
    assert (await _admit(replace(on), tenant, "chan-2")).backend_revision is None


@pytest.mark.parametrize("enabled", [None, "false"])
def test_selfhosted_admission_is_disabled_by_default_or_explicit_switch(
    monkeypatch, tmp_path, enabled
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DAIMON_SELFHOSTED", raising=False)
    monkeypatch.delenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", raising=False)
    if enabled is not None:
        monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", enabled)
    with (
        capture_logs() as records,
        pytest.raises(BackendUnsupported, match="selfhosted_admission_disabled"),
    ):
        check_backend(
            _revision(
                BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna")
            )
        )

    assert records[-1]["event"] == "turn.backend_unsupported"
    assert records[-1]["reason"] == "selfhosted_admission_disabled"


def test_selfhosted_admission_enabled_preserves_required_usage_capability(monkeypatch):
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    admitted = check_backend(
        _revision(
            BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna")
        )
    )
    assert admitted.provider == "selfhosted"
    assert "usage_observations" in admitted.satisfied


def test_selfhosted_admission_enabled_refuses_missing_usage_capability(monkeypatch):
    from daimon.core import channel_backend
    from mux.profiles import get_profile

    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    profile = get_profile("selfhosted.loop")
    unmetered = profile.model_copy(
        update={"support": {**profile.support, "usage_observations": "unsupported"}}
    )
    monkeypatch.setattr(channel_backend, "get_profile", lambda name: unmetered)
    with pytest.raises(BackendUnsupported, match="usage_observations"):
        check_backend(
            _revision(
                BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna")
            )
        )

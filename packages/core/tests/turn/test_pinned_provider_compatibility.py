"""Provider keys and saved choices do not change explicitly pinned legacy execution."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from daimon.core._models import AgentBackendRevisionRecord
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_backend import AgentBackendRefused, set_channel_backend
from daimon.core.config import load_settings
from daimon.core.fleet_configuration import is_default_fleet
from daimon.core.neutral_resources import WorkerSpecRefused
from daimon.core.stores.access_policy import (
    clear_access_policy,
    load_access_policy,
    set_access_policy,
)
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtime_manifest import read_agent_fleet_grant, read_frozen_root
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig
from pydantic import ValidationError
from sqlalchemy import delete

from .native_counterpart_support import db_preflight as db_preflight
from .test_backend_selection import (
    initial_backend as initial_backend,
)
from .test_backend_selection import (
    preference_for,
)
from .test_backend_selection import (
    selector_host as selector_host,
)
from .test_frozen_root import freeze
from .test_frozen_root import root_turn as root_turn
from .test_provider_dispatch import ACCOUNT, PROFILE, REVISION, TENANT


@pytest.fixture(autouse=True)
def pinned_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DAIMON_CHANNEL_OPENAI_KEY", "DAIMON_CHANNEL_GEMINI_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DAIMON_TURN__PATH", "legacy")
    monkeypatch.setenv("DAIMON_TURN__CHANNEL_BACKENDS", "false")
    monkeypatch.setenv("DAIMON_TURN__AGENT_BACKENDS", "false")
    monkeypatch.setenv("DAIMON_FLEET__ENABLED", "false")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "synthetic-key")
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://offline:offline@localhost/db")


def _legacy_deps(host):
    settings = load_settings(_env_file=None)
    assert (
        settings.turn.path,
        settings.turn.channel_backends,
        settings.turn.agent_backends,
        settings.fleet.enabled,
    ) == ("legacy", False, False, False), "the deployment pins should load as legacy/off"
    return replace(
        host.deps,
        turn_path=settings.turn.path,
        channel_backends=settings.turn.channel_backends,
        agent_backends=settings.turn.agent_backends,
        fleet_enabled=settings.fleet.enabled,
    )


async def _admit(host, thread_id: str):
    return await admit_impl(
        _legacy_deps(host),
        tenant_id=TENANT,
        requester_org=RequesterOrg.HOST,
        platform="slack",
        external_user_id="caller",
        channel_id="channel",
        thread_id=thread_id,
        now=datetime.now(UTC),
    )


async def _run_legacy_turn(host, thread_id: str) -> None:
    admission = await _admit(host, thread_id)
    assert admission.backend_revision is None, "legacy admission must ignore native choices"
    deps = _legacy_deps(host)
    prepared = await bind_session_impl(
        deps,
        admission,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id=thread_id,
        session_account_id=ACCOUNT,
        reuse_existing=True,
    )

    async def reseed_user_message() -> str:
        return "compatibility question"

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id=thread_id,
        external_user_id="caller",
        user_message="compatibility question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed_user_message,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        render_interval_s=0.01,
        operation_key=f"pinned-provider-{thread_id}",
    )
    await drain_outcomes()

    assert outcome.state.error is None, "the pinned legacy turn should complete"
    assert "Hello from the agent!" in str(outcome.state.content), (
        "the scripted MA response should reach output"
    )
    sends = [
        request
        for request in host.calls
        if request.url.path.endswith("/events") and request.method == "POST"
    ]
    assert len(sends) == 1, "one user message should be sent to Managed Agents"
    assert all(request.url.host == "anthropic.invalid" for request in host.calls), (
        "the scripted MA transport should receive only MA requests"
    )
    assert host.wire.requests == [], "the OpenAI transport must receive no request"


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
@pytest.mark.parametrize("selector", ["none", "channel", "agent"])
@pytest.mark.parametrize("keys", [(), ("openai",), ("gemini",), ("openai", "gemini")])
async def test_legacy_pin_keeps_managed_agents_execution_with_saved_native_choices(
    selector_host, monkeypatch: pytest.MonkeyPatch, selector: str, keys: tuple[str, ...]
) -> None:
    host = selector_host
    for provider in keys:
        monkeypatch.setenv(f"DAIMON_CHANNEL_{provider.upper()}_KEY", "synthetic-key")
    async with host.sessions.begin() as db:
        await clear_access_policy(db, tenant_id=TENANT)
    if selector == "channel":
        first = (
            BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash")
            if keys == ("gemini",)
            else BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna")
        )
        async with host.sessions.begin() as db:
            saved = await set_channel_backend(db, REVISION.channel, first)
            assert saved.backend == first.backend, "a channel choice should save with the flag off"
            if len(keys) == 2:
                changed = await set_channel_backend(
                    db,
                    REVISION.channel,
                    BackendConfig(
                        backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
                    ),
                )
                assert changed.backend == "gemini" and changed.local > saved.local, (
                    "a later channel choice should be saved"
                )
    elif selector == "agent":
        service, agent, actor = preference_for(host)
        if keys:
            first_provider = "gemini" if keys == ("gemini",) else "openai"
            revision = await service.set_backend(agent, first_provider, 0, actor)
            assert revision.backend == first_provider, "the keyed choice should be saved"
            if len(keys) == 2:
                changed = await service.set_backend(agent, "gemini", revision.local, actor)
                assert changed.backend == "gemini" and changed.local > revision.local, (
                    "an agent choice should change"
                )
        else:
            with pytest.raises(AgentBackendRefused, match="backend not allowed"):
                await service.set_backend(agent, "openai", 0, actor)

    async with host.sessions() as db:
        policy = await load_access_policy(db, tenant_id=TENANT)
    expected = ("anthropic", *keys)
    assert policy.enabled_backends == expected, "default policy should reflect deployment keys"
    assert not policy.backends_explicit, "the default should remain a read-time choice"

    await _run_legacy_turn(host, f"thread-{selector}")


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_explicit_restricted_policy_and_tenant_default_stay_separate(
    selector_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = selector_host
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "synthetic-key")
    other_tenant = uuid.uuid4()
    async with host.sessions.begin() as db:
        await clear_access_policy(db, tenant_id=TENANT)
        # The fixture owns only one tenant, so make another through the real store factory.
        from daimon.testing.factories import make_tenant

        await make_tenant(db, id=other_tenant)
        await set_access_policy(
            db, tenant_id=TENANT, policy=TenantAccessPolicy(enabled_backends=("anthropic",))
        )
    async with host.sessions() as db:
        restricted = await load_access_policy(db, tenant_id=TENANT)
        default = await load_access_policy(db, tenant_id=other_tenant)
    assert restricted.enabled_backends == ("anthropic",), "explicit restriction should persist"
    assert restricted.backends_explicit, "the restriction should not follow new keys"
    assert default.enabled_backends == ("anthropic", "openai"), (
        "the other tenant should follow keys"
    )
    assert not default.backends_explicit, "another tenant should remain on the default"
    async with host.sessions.begin() as db:
        await set_access_policy(
            db,
            tenant_id=other_tenant,
            policy=TenantAccessPolicy(enabled_backends=("anthropic", "openai")),
        )
    monkeypatch.delenv("DAIMON_CHANNEL_OPENAI_KEY")
    async with host.sessions() as db:
        chosen = await load_access_policy(db, tenant_id=other_tenant)
    assert chosen.enabled_backends == ("anthropic", "openai"), (
        "an explicit choice survives key removal"
    )
    assert chosen.backends_explicit, "the chosen list must remain explicit"
    service, agent, actor = preference_for(host)
    with pytest.raises(AgentBackendRefused, match="backend not allowed"):
        await service.set_backend(agent, "openai", 0, actor)
    with pytest.raises(AgentBackendRefused, match="tenant mismatch"):
        await service.set_backend(agent, "openai", 0, replace(actor, tenant_id=other_tenant))
    await _run_legacy_turn(host, "restricted-thread")


@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_explicit_native_allowlist_still_runs_the_legacy_provider(
    selector_host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = selector_host
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "synthetic-key")
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "synthetic-key")
    async with host.sessions.begin() as db:
        await set_access_policy(
            db,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(enabled_backends=("anthropic", "openai", "gemini")),
        )
        channel = await set_channel_backend(
            db,
            REVISION.channel,
            BackendConfig(
                backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
            ),
        )
    assert channel.backend == "gemini", "the explicit policy should permit a saved channel choice"
    service, agent, actor = preference_for(host)
    selected = await service.set_backend(agent, "openai", 0, actor)
    assert selected.backend == "openai", "the explicit policy should permit an agent choice"
    async with host.sessions() as db:
        chosen = await load_access_policy(db, tenant_id=TENANT)
    assert chosen.enabled_backends == ("anthropic", "openai", "gemini"), (
        "the explicit list should include both keyed providers"
    )
    assert chosen.backends_explicit, "the stored list must not be mistaken for a default"
    await _run_legacy_turn(host, "explicit-native-thread")


def test_explicit_empty_allowlist_is_rejected_by_policy_validation() -> None:
    with pytest.raises(ValidationError, match="requires Anthropic"):
        TenantAccessPolicy(enabled_backends=())


@pytest.mark.parametrize("keys", [(), ("openai", "gemini")])
async def test_fleet_off_pin_refuses_an_admitted_root_whatever_the_keys(
    root_turn, monkeypatch: pytest.MonkeyPatch, keys: tuple[str, ...]
) -> None:
    """The fleet-off pin alone refuses a real Discord root that fleet-on admits."""
    for provider in keys:
        monkeypatch.setenv(f"DAIMON_CHANNEL_{provider.upper()}_KEY", "synthetic-key")
    deps, prepared, _, intent, _, scope, *_ = root_turn
    assert deps.fleet_enabled, "the fixture's root is built with fleet on"
    pinned = replace(deps, fleet_enabled=load_settings(_env_file=None).fleet.enabled)
    assert not pinned.fleet_enabled, "the deployment pin should load fleet off"
    async with deps.sessionmaker.begin() as db:
        await db.execute(delete(AgentBackendRevisionRecord))
    now = datetime.now(UTC)
    on_grant = await read_agent_fleet_grant(deps, prepared.admission, now=now)
    assert on_grant is not None and is_default_fleet(on_grant), "fleet on resolves the default"
    assert await read_agent_fleet_grant(pinned, prepared.admission, now=now) is None, (
        "the fleet-off pin must refuse the grant"
    )
    await freeze(root_turn)
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(pinned, scope, intent_id=intent.id, now=datetime.now(UTC))
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    assert parent.fleet.depth == 0, "the same frozen root is readable with fleet on"

"""The operator entrypoint retains production admission, continuation and metering."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from daimon.adapters.cli.commands.selfhosted import operator_turn


@pytest.mark.parametrize("backend", [None, "anthropic"])
async def test_refuses_unconfigured_or_foreign_backend(monkeypatch, backend):
    import daimon.core.turn as turn
    from daimon.core import channel_backend

    session = AsyncMock()
    rt = SimpleNamespace(sessionmaker=lambda: session)
    monkeypatch.setattr(
        channel_backend,
        "current_backend",
        AsyncMock(return_value=None if backend is None else SimpleNamespace(backend=backend)),
    )
    admitted = AsyncMock()
    monkeypatch.setattr(turn, "admit", admitted)
    with pytest.raises(ValueError, match="explicitly select selfhosted"):
        await operator_turn(
            rt, tenant_id=uuid4(), channel="test", thread="same", caller="op", message="hi"
        )
    admitted.assert_not_called()


async def test_uses_production_chokepoints_and_reuses_thread(monkeypatch):
    import daimon.core.turn as turn
    from daimon.core import channel_backend
    from daimon.core.turn import deps

    account = uuid4()
    admitted = SimpleNamespace(
        account_id=account, backend_revision=SimpleNamespace(backend="selfhosted")
    )
    prepared = SimpleNamespace(ma_session_id="session")
    result = object()
    session = AsyncMock()
    rt = SimpleNamespace(
        sessionmaker=lambda: session,
        settings=object(),
        anthropic=object(),
        deployment_default=object(),
        resolver_cache=object(),
    )
    monkeypatch.setattr(
        channel_backend,
        "current_backend",
        AsyncMock(return_value=SimpleNamespace(backend="selfhosted")),
    )
    built = object()
    monkeypatch.setattr(deps, "build_turn_deps", lambda *a, **kw: built)
    admit = AsyncMock(return_value=admitted)
    bind = AsyncMock(return_value=prepared)
    run = AsyncMock(return_value=result)
    monkeypatch.setattr(turn, "admit", admit)
    monkeypatch.setattr(turn, "bind_session", bind)
    monkeypatch.setattr(turn, "run_prepared_turn", run)
    assert (
        await operator_turn(
            rt, tenant_id=uuid4(), channel="test", thread="same", caller="op", message="hi"
        )
        is result
    )
    assert bind.call_args.kwargs["reuse_existing"] is True
    assert bind.call_args.kwargs["session_account_id"] == account
    assert run.call_args.args == (built, prepared)
    assert "billing" not in run.call_args.kwargs
    assert run.call_args.kwargs["thread_id"] == "same"


@pytest.mark.parametrize("channel_backends", [False, True])
async def test_real_admission_refuses_disabled_selection_or_retained_anthropic_thread(
    db_session, db_session_factory, monkeypatch, channel_backends
):
    from dataclasses import replace

    from daimon.core import channel_backend
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.anthropic_guard import anthropic_disabled
    from daimon.core.stores import mux_state
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.core.turn import deps
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.outcomes import drain_outcomes
    from daimon.testing.factories import (
        make_platform_principal,
        make_tenant,
        make_tenant_config,
        make_thread_session,
    )
    from daimon.testing.turn_fakes import deps_for
    from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default

    class ForbiddenAnthropic:
        def __getattr__(self, name):
            raise AssertionError(f"Provider access before refusal: {name}")

    tenant = await make_tenant(db_session)
    principal = await make_platform_principal(
        db_session, tenant=tenant, platform="cli", external_id="op"
    )
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(enabled_backends=("anthropic", "selfhosted")),
    )
    revision = ConfigRevision.create(
        channel_backend.channel_ref(tenant.id, "cli", "test"),
        1,
        resolve_default(
            BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna")
        ),
    )
    await mux_state.put_config_revision(db_session, revision)
    if channel_backends:
        from daimon.core.stores.accounts import get_account

        account = await get_account(db_session, principal.account_id)
        assert account is not None
        await make_thread_session(
            db_session,
            tenant=tenant,
            account=account,
            platform="cli",
            thread_id="same",
            channel_id="test",
            ma_agent_id="old-anthropic-agent",
        )
    await db_session.commit()
    built = replace(
        deps_for(ForbiddenAnthropic(), db_session_factory), channel_backends=channel_backends
    )
    monkeypatch.setattr(deps, "build_turn_deps", lambda *a, **kw: built)
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    rt = SimpleNamespace(
        sessionmaker=db_session_factory,
        settings=object(),
        anthropic=built.anthropic,
        deployment_default=built.deployment_default,
        resolver_cache=built.resolver_cache,
    )
    # Neither admit nor provider resolution is mocked: the real retained-thread
    # and feature-flag selection must refuse before touching the sentinel client.
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await operator_turn(
            rt, tenant_id=tenant.id, channel="test", thread="same", caller="op", message="hi"
        )
    assert not anthropic_disabled()
    await drain_outcomes()

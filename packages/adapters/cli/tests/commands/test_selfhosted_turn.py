"""The operator entrypoint retains production admission, continuation and metering."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from daimon.adapters.cli.commands.selfhosted import operator_turn


@pytest.mark.parametrize("backend", [None, "anthropic"])
async def test_refuses_unconfigured_or_foreign_backend(monkeypatch, backend):
    from daimon.core import channel_backend
    from daimon.core.turn import admission

    session = AsyncMock()
    rt = SimpleNamespace(sessionmaker=lambda: session)
    monkeypatch.setattr(
        channel_backend,
        "current_backend",
        AsyncMock(return_value=None if backend is None else SimpleNamespace(backend=backend)),
    )
    admitted = AsyncMock()
    monkeypatch.setattr(admission, "admit", admitted)
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
    admitted = SimpleNamespace(account_id=account)
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

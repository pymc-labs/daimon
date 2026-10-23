"""The turn leg is a real input to the fixtures, and each leg changes what runs.

`deps_for` takes its path and backend flags from `daimon.testing.turn_leg`, which
reads `DAIMON_TURN__PATH` / `DAIMON_TURN__CHANNEL_BACKENDS` through the product
loader. These tests assert the seams those flags reach:

* `run._turn_port_kwargs`: which I/O path a turn is dispatched on;
* `runtimes.build_channel_runtime`: whether a non-Anthropic channel backend is
  admitted past the path/backend gate or refused as `backend_unsupported`.

Tests named `*_both_legs` pin each leg themselves and run on every CI leg; the
`*_selected_leg` tests follow the leg the run chose.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from daimon.core.config import TurnSettings
from daimon.core.turn import runtimes as runtime_module
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.turn.run import _turn_port_kwargs  # pyright: ignore[reportPrivateUsage]
from daimon.testing.turn_fakes import deps_for
from daimon.testing.turn_leg import LEGACY_LEG, MUX_LEG, turn_leg

from .test_provider_dispatch import ACCOUNT, SCOPE, SESSION, TENANT, admission

LEGS = [pytest.param(LEGACY_LEG, id="legacy"), pytest.param(MUX_LEG, id="mux")]


def _set_leg(monkeypatch: pytest.MonkeyPatch, leg: TurnSettings) -> None:
    monkeypatch.setenv("DAIMON_TURN__PATH", leg.path)
    monkeypatch.setenv("DAIMON_TURN__CHANNEL_BACKENDS", str(leg.channel_backends).lower())


@pytest.mark.parametrize("leg", LEGS)
def test_deps_for_follows_the_environment_both_legs(
    monkeypatch: pytest.MonkeyPatch, leg: TurnSettings
) -> None:
    _set_leg(monkeypatch, leg)

    deps = deps_for()

    assert (deps.turn_path, deps.channel_backends) == (leg.path, leg.channel_backends)


def test_deps_for_follows_the_product_default_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAIMON_TURN__PATH", raising=False)
    monkeypatch.delenv("DAIMON_TURN__CHANNEL_BACKENDS", raising=False)
    monkeypatch.delenv("DAIMON_TURN__AGENT_BACKENDS", raising=False)

    deps = deps_for()

    assert (deps.turn_path, deps.channel_backends, deps.agent_backends) == ("mux", True, True)


def test_explicit_leg_overrides_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_leg(monkeypatch, LEGACY_LEG)

    assert deps_for(leg=MUX_LEG).turn_path == "mux"


@pytest.mark.parametrize("leg", LEGS)
def test_turn_dispatch_path_for_an_anthropic_turn_both_legs(leg: TurnSettings) -> None:
    anthropic = replace(admission(), backend_revision=None)

    kwargs = _turn_port_kwargs(
        deps_for(leg=leg), anthropic, "session", tenant_id=TENANT, provider_scope=SCOPE
    )

    # The legacy answer omits `path` (run_turn's default); mux names itself.
    assert kwargs.get("path", "legacy") == leg.path


@pytest.mark.parametrize("leg", LEGS)
def test_turn_dispatch_names_the_profile_of_a_native_turn_both_legs(leg: TurnSettings) -> None:
    kwargs = _turn_port_kwargs(
        deps_for(leg=leg),
        admission(),
        "session",
        tenant_id=TENANT,
        provider_session=SESSION,
        provider_scope=SCOPE,
    )

    assert kwargs["path"] == leg.path
    assert kwargs["profile"] == admission().backend_revision.profile


def _request(leg: TurnSettings) -> ProviderPreparationRequest:
    return ProviderPreparationRequest(
        deps_for(leg=leg),
        admission(),
        SCOPE,
        TENANT,
        "slack",
        "caller",
        "thread",
        ACCOUNT,
        True,
        cast(object, None),
        None,
        datetime.now(UTC) + timedelta(seconds=20),
        lambda: datetime.now(UTC),
    )


@pytest.mark.parametrize("leg", LEGS)
def test_channel_backend_admission_gate_both_legs(
    monkeypatch: pytest.MonkeyPatch, leg: TurnSettings
) -> None:
    """Legacy refuses at the gate; mux passes it and reaches the profile's runtime."""
    reached_runtime_lookup: list[str] = []

    def runtime_unavailable(profile: str, **_: object) -> bool:
        reached_runtime_lookup.append(profile)
        return False

    monkeypatch.setattr(runtime_module, "ensure_channel_runtime", runtime_unavailable)

    with pytest.raises(AdmissionDenied) as refused:
        runtime_module.build_channel_runtime(_request(leg))

    assert refused.value.reason == "backend_unsupported"
    assert bool(reached_runtime_lookup) == (leg.path == "mux" and leg.channel_backends)


def test_channel_backend_admission_follows_the_selected_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = turn_leg()
    seen: list[ProviderPreparationRequest] = []

    def runtime_unavailable(profile: str, **_: object) -> bool:
        seen.append(request)
        return False

    request = _request(selected)
    monkeypatch.setattr(runtime_module, "ensure_channel_runtime", runtime_unavailable)

    with pytest.raises(AdmissionDenied):
        runtime_module.build_channel_runtime(request)

    assert request.deps.turn_path == selected.path
    assert request.deps.channel_backends == selected.channel_backends
    assert bool(seen) == (selected.path == "mux" and selected.channel_backends)

"""The turn leg is a real input to the fixtures, and its backend flags change what runs.

`deps_for` takes its backend flags from `daimon.testing.turn_leg`, which reads
`DAIMON_TURN__CHANNEL_BACKENDS` / `DAIMON_TURN__AGENT_BACKENDS` through the
product loader. These tests assert the seams those flags reach:

* `run._turn_port_kwargs`: which profile a turn is dispatched on;
* `runtimes.build_channel_runtime`: whether a non-Anthropic channel backend is
  admitted past the backend gate or refused as `backend_unsupported`.
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
from daimon.testing.turn_leg import MUX_LEG, turn_leg

from .test_provider_dispatch import ACCOUNT, SCOPE, SESSION, TENANT, admission

OFF_LEG = TurnSettings(channel_backends=False, agent_backends=False)
LEGS = [pytest.param(OFF_LEG, id="off"), pytest.param(MUX_LEG, id="on")]


@pytest.mark.parametrize("leg", LEGS)
def test_deps_for_follows_the_environment(
    monkeypatch: pytest.MonkeyPatch, leg: TurnSettings
) -> None:
    monkeypatch.setenv("DAIMON_TURN__CHANNEL_BACKENDS", str(leg.channel_backends).lower())

    assert deps_for().channel_backends == leg.channel_backends


def test_deps_for_follows_the_product_default_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAIMON_TURN__CHANNEL_BACKENDS", raising=False)
    monkeypatch.delenv("DAIMON_TURN__AGENT_BACKENDS", raising=False)

    deps = deps_for()

    assert (deps.channel_backends, deps.agent_backends) == (True, True)


def test_explicit_leg_overrides_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_TURN__CHANNEL_BACKENDS", "false")

    assert deps_for(leg=MUX_LEG).channel_backends is True


def test_an_anthropic_turn_names_no_profile() -> None:
    anthropic = replace(admission(), backend_revision=None)

    kwargs = _turn_port_kwargs(
        deps_for(), anthropic, "session", tenant_id=TENANT, provider_scope=SCOPE
    )

    assert "profile" not in kwargs


def test_turn_dispatch_names_the_profile_of_a_native_turn() -> None:
    kwargs = _turn_port_kwargs(
        deps_for(),
        admission(),
        "session",
        tenant_id=TENANT,
        provider_session=SESSION,
        provider_scope=SCOPE,
    )

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


@pytest.mark.parametrize("leg", [*LEGS, None])
def test_channel_backend_admission_gate(
    monkeypatch: pytest.MonkeyPatch, leg: TurnSettings | None
) -> None:
    """Off refuses at the gate; on passes it and reaches the profile's runtime.

    `None` follows the leg the run selected.
    """
    selected = leg if leg is not None else turn_leg()
    reached_runtime_lookup: list[str] = []

    def runtime_unavailable(profile: str) -> None:
        reached_runtime_lookup.append(profile)

    monkeypatch.setattr(runtime_module, "hosted_backend", runtime_unavailable)

    with pytest.raises(AdmissionDenied) as refused:
        runtime_module.build_channel_runtime(_request(selected))

    assert refused.value.reason == "backend_unsupported"
    assert bool(reached_runtime_lookup) == selected.channel_backends

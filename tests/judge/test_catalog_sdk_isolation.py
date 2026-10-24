"""Public replay and real catalog admission use the shared isolated SDKs."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx
import pytest
from daimon.testing.provider_fixtures import load_provider_fixtures, turn_tape
from daimon.testing.provider_replay import provider_replay
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from mux.testing.isolated_sdk import SDK_ENV_VARS, AmbientSDKConfigurationError

if TYPE_CHECKING:
    pass

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "packages/testing/fixtures/target53"
CATALOG = DATA / "catalog"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=CATALOG)
SCENARIO_ID = "QA-NEW37-MARKDOWN-TABLE-READABLE"


class NamesOnly(Mapping[str, str]):
    """Match real Mapping membership semantics; fail on present-value reads."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.reads: list[str] = []

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if key == self.name:
            self.reads.append(key)
            raise AssertionError("present ambient value was read")
        raise KeyError(key)


def no_http_client(*args: object, **kwargs: object) -> None:
    raise AssertionError("ambient option reached HTTP client allocation")


@pytest.mark.parametrize("backend", ("openai", "gemini"))
@pytest.mark.parametrize("name", SDK_ENV_VARS)
@pytest.mark.parametrize("value", ("", "invented-test-option"))
async def test_public_provider_replay_refuses_before_http_allocation(
    monkeypatch: pytest.MonkeyPatch,
    backend: Literal["openai", "gemini"],
    name: str,
    value: str,
) -> None:
    scenario, fixture = PACK.select(SCENARIO_ID, backend)
    tape = turn_tape(scenario, fixture, fixture.turns[0], key="offline")
    ambient = {name: value}
    monkeypatch.setattr(os, "environ", ambient)
    monkeypatch.setattr(httpx, "AsyncClient", no_http_client)
    monkeypatch.setattr(httpx, "Client", no_http_client)
    try:
        async with provider_replay(
            tape, backend=backend, profile=fixture.profile, model=fixture.model, source_root=CATALOG
        ):
            raise AssertionError("ambient replay yielded a client")
    except AmbientSDKConfigurationError as error:
        if name not in str(error) or "invented-test-option" in str(error):
            raise AssertionError("refusal diagnostic did not use names only") from error
    else:
        raise AssertionError("ambient replay was admitted")
    if ambient != {name: value}:
        raise AssertionError("factory mutated the environment")


@pytest.mark.parametrize("name", SDK_ENV_VARS)
async def test_scripted_sentinel_uses_the_shared_name_only_guard(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    ambient = NamesOnly(name)
    wire = ScriptedTransport()
    with monkeypatch.context() as guarded:
        guarded.setattr(os, "environ", ambient)
        guarded.setattr(httpx, "AsyncClient", no_http_client)
        try:
            wire.client()
        except AmbientSDKConfigurationError:
            if ambient.reads or wire.requests or wire.violations:
                raise AssertionError("sentinel refusal read values or performed I/O") from None
        else:
            raise AssertionError("ambient sentinel was admitted")

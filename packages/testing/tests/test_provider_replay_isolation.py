"""Provider replay adopts the shared SDK preflight before any request."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pytest
from daimon.testing.provider_fixtures import load_provider_fixtures, turn_tape
from daimon.testing.provider_replay import Backend, ProviderTape, WireReplay, provider_replay
from mux.testing.isolated_sdk import SDK_ENV_VARS, AmbientSDKConfigurationError

DATA = Path(__file__).resolve().parents[3] / "packages/testing/fixtures/target53"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")


def tape_for(backend: Backend) -> ProviderTape:
    for scenario in PACK.scenarios:
        for fixture in scenario.providers:
            if fixture.backend == backend and fixture.turns:
                return turn_tape(scenario, fixture, fixture.turns[0], key="qa-send")
    raise RuntimeError("authored native provider fixture is required")


@pytest.mark.parametrize("backend", ["openai", "gemini"])
@pytest.mark.parametrize("name", SDK_ENV_VARS)
async def test_replay_refuses_ambient_settings_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, name: str
) -> None:
    # Replace the mapping without reading any operator environment values.
    monkeypatch.setattr(os, "environ", {name: "offline-ambient-sentinel"})

    def refuse_dispatch(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ambient configuration reached replay I/O")

    monkeypatch.setattr(WireReplay, "dispatch", refuse_dispatch)
    tape = tape_for(backend)
    with pytest.raises(AmbientSDKConfigurationError):
        async with provider_replay(
            tape,
            backend=backend,
            profile=tape.profile,
            model=tape.model,
            source_root=DATA / "catalog",
        ):
            raise RuntimeError("ambient configuration reached an allocated replay")


class NamesOnly(Mapping[str, str]):
    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value
        self.reads: list[str] = []

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if key == self.name:
            self.reads.append(key)
            raise RuntimeError("future provider setting value was read")
        raise KeyError(key)


@pytest.mark.parametrize("backend", ["openai", "gemini"])
@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_FUTURE_SETTING",
        "openai_future_setting",
        "ANTHROPIC_FUTURE_SETTING",
        "anthropic_future_setting",
        "GOOGLE_FUTURE_SETTING",
        "Google_Future_Setting",
        "GEMINI_FUTURE_SETTING",
        "Gemini_Future_Setting",
    ],
)
@pytest.mark.parametrize("value", ["", "offline-future-option"])
async def test_replay_refuses_future_provider_settings_before_allocation(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, name: str, value: str
) -> None:
    ambient = NamesOnly(name, value)

    def refuse_allocation(*args: object, **kwargs: object) -> None:
        raise RuntimeError("future provider setting reached HTTP allocation or replay I/O")

    tape = tape_for(backend)
    with monkeypatch.context() as guarded:
        guarded.setattr(os, "environ", ambient)
        guarded.setattr(httpx, "Client", refuse_allocation)
        guarded.setattr(httpx, "AsyncClient", refuse_allocation)
        guarded.setattr(WireReplay, "dispatch", refuse_allocation)
        with pytest.raises(AmbientSDKConfigurationError) as refused:
            async with provider_replay(
                tape,
                backend=backend,
                profile=tape.profile,
                model=tape.model,
                source_root=DATA / "catalog",
            ):
                raise RuntimeError("future ambient configuration reached a replay")
    if ambient.reads or str(refused.value) != "test SDK refuses ambient provider configuration":
        raise RuntimeError("future setting refusal must avoid value reads and redact unknown names")

"""Replay admission refuses ambient SDK options without reading any values."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
import pytest
from daimon.testing.anthropic_fixtures import (
    anthropic_replay,
    anthropic_turn_tape,
    load_anthropic_fixtures,
)
from daimon.testing.provider_fixtures import load_provider_fixtures, turn_tape
from daimon.testing.provider_replay import provider_replay
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "packages/testing/fixtures/target53"
NATIVE = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")
ANTHROPIC = load_anthropic_fixtures(DATA / "anthropic/index.json", source_root=ROOT)
type Caller = Literal["openai", "gemini", "anthropic", "sentinel"]


class NamesOnly(Mapping[str, str]):
    def __init__(self, name: str) -> None:
        self.name = name
        self.reads = 0

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if key != self.name:
            raise KeyError(key)
        self.reads += 1
        raise AssertionError("replay admission read an environment value")


@asynccontextmanager
async def admit(caller: Caller) -> AsyncIterator[None]:
    if caller == "openai" or caller == "gemini":
        scenario, fixture = NATIVE.select("QA-NEW37-MARKDOWN-TABLE-READABLE", caller)
        tape = turn_tape(scenario, fixture, fixture.turns[0], key="offline-isolation-control")
        async with provider_replay(
            tape,
            backend=caller,
            profile=fixture.profile,
            model=fixture.model,
            source_root=DATA / "catalog",
        ):
            yield
    elif caller == "anthropic":
        scenario = next(
            s for s in ANTHROPIC.scenarios if s.scenario_id == "QA-NEW37-MARKDOWN-TABLE-READABLE"
        )
        entry = next(e for e in ANTHROPIC.index.entries if e.scenario_id == scenario.scenario_id)
        tape = anthropic_turn_tape(entry, scenario, scenario.turns[0], codec=ANTHROPIC.index.codec)
        async with anthropic_replay(tape, source_root=DATA / "anthropic", codec_root=ROOT):
            yield
    else:
        async with ScriptedTransport().client():
            yield


@pytest.mark.parametrize("caller", ("openai", "gemini", "anthropic", "sentinel"))
@pytest.mark.parametrize(
    "name",
    (
        "OPENAI_ADMIN_KEY",
        "OPENAI_WEBHOOK_SECRET",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_CUSTOM_HEADERS",
        "ANTHROPIC_WEBHOOK_SIGNING_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_BASE_URL",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_GEMINI_BASE_URL",
    ),
)
async def test_owned_replay_and_host_sentinel_refuse_before_http_allocation(
    caller: Caller, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = NamesOnly(name)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("replay allocated HTTP before ambient refusal")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", environment)
        scoped.setattr(httpx, "Client", forbidden)
        scoped.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(ValueError, match="refuses ambient"):
            async with admit(caller):
                raise AssertionError("replay admitted ambient SDK options")
    if environment.reads:
        raise AssertionError("names-only replay preflight fetched a value")


@pytest.mark.parametrize("caller", ("openai", "gemini", "anthropic", "sentinel"))
@pytest.mark.parametrize("value", ("", "invented-never-send"))
async def test_present_empty_or_nonempty_custom_headers_never_admit(
    caller: Caller, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = {"OPENAI_CUSTOM_HEADERS": value}
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", environment)
        with pytest.raises(ValueError, match="refuses ambient") as caught:
            async with admit(caller):
                raise AssertionError("custom headers reached a replay client")
    if value and value in str(caught.value):
        raise AssertionError("refusal exposed a synthetic credential")
    if environment != {"OPENAI_CUSTOM_HEADERS": value}:
        raise AssertionError("replay mutated the caller environment")

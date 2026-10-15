"""Gemini host cases; shared helpers belong to test_catalog_sdk_isolation."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal

import httpx
import pytest
from daimon.testing.isolated_sdk import SDK_ENV_VARS

from .catalog_runner import load_catalog, plan_scenario
from .headless_executor import execute_gemini_cell, execute_openai_cell

if TYPE_CHECKING:
    from google.genai import Client
    from openai import AsyncOpenAI

from .test_catalog_sdk_isolation import (
    CATALOG as CATALOG,
)
from .test_catalog_sdk_isolation import (
    PACK as PACK,
)
from .test_catalog_sdk_isolation import (
    ROOT as ROOT,
)
from .test_catalog_sdk_isolation import (
    SCENARIO_ID as SCENARIO_ID,
)
from .test_catalog_sdk_isolation import (
    NamesOnly as NamesOnly,
)
from .test_catalog_sdk_isolation import (
    no_http_client as no_http_client,
)


@pytest.mark.parametrize("backend", ("gemini",))
@pytest.mark.parametrize("name", SDK_ENV_VARS)
async def test_real_catalog_path_refuses_without_reading_present_values(
    monkeypatch: pytest.MonkeyPatch,
    backend: Literal["openai", "gemini"],
    name: str,
) -> None:
    scenario, fixture = PACK.select(SCENARIO_ID, backend)
    entry = next(e for e in load_catalog(CATALOG) if e.id == SCENARIO_ID).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, backend, run_id="isolated")
    ambient = NamesOnly(name)
    with monkeypatch.context() as guarded:
        guarded.setattr(os, "environ", ambient)
        guarded.setattr(httpx, "AsyncClient", no_http_client)
        guarded.setattr(httpx, "Client", no_http_client)
        try:
            await (execute_openai_cell if backend == "openai" else execute_gemini_cell)(
                plan, scenario, fixture, source_root=CATALOG, repository_root=ROOT
            )
        except ValueError:
            if ambient.reads:
                raise AssertionError("catalog refusal fetched an ambient value") from None
        else:
            raise AssertionError("ambient catalog path was admitted")


@pytest.mark.parametrize("backend", ("gemini",))
async def test_clean_factory_preserves_exact_native_tape_and_closes_clients(
    monkeypatch: pytest.MonkeyPatch, backend: Literal["openai", "gemini"]
) -> None:
    from daimon.testing import provider_replay as factory

    monkeypatch.setattr(os, "environ", {})
    owned: list[httpx.Client | httpx.AsyncClient] = []
    # Track explicit clients without replacing SDK-visible client types.
    if backend == "openai":
        original = factory.build_async_openai

        def build_openai(**kwargs: Any) -> AsyncOpenAI:
            client = original(**kwargs)
            owned.append(client._client)  # pyright: ignore[reportPrivateUsage]
            return client

        monkeypatch.setattr(factory, "build_async_openai", build_openai)
    else:
        original_gemini = factory.build_gemini_client

        def build_gemini(**kwargs: Any) -> Client:
            client = original_gemini(**kwargs)
            sync_http = client._api_client._httpx_client  # pyright: ignore[reportPrivateUsage]
            async_http = client._api_client._async_httpx_client  # pyright: ignore[reportPrivateUsage]
            if not isinstance(sync_http, httpx.Client) or not isinstance(
                async_http, httpx.AsyncClient
            ):
                raise AssertionError("factory did not own both HTTP clients")
            owned.extend((sync_http, async_http))
            return client

        monkeypatch.setattr(factory, "build_gemini_client", build_gemini)
    scenario, fixture = PACK.select(SCENARIO_ID, backend)
    entry = next(e for e in load_catalog(CATALOG) if e.id == SCENARIO_ID).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, backend, run_id="clean-isolation")
    result = await (execute_openai_cell if backend == "openai" else execute_gemini_cell)(
        plan, scenario, fixture, source_root=CATALOG, repository_root=ROOT
    )
    if result.request_count != (6 if backend == "openai" else 3):
        raise AssertionError("isolation changed exact request consumption")
    if result.reported_status != "EXPECTED-FAIL" or result.host_outcome.status != "PASS":
        raise AssertionError("isolation changed the honest table baseline")
    if not owned or any(not c.is_closed for c in owned):
        raise AssertionError("replay leaked an owned HTTP client")

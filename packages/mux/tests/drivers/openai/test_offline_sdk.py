"""Surrogate credential/header refusal at actual factories and SDK copies."""

import os
from collections.abc import Iterator, Mapping

import httpx
import pytest
from mux.contracts.resources import MCPConnection
from mux.drivers.openai.transport import SDKTransport
from mux.testing.conformance.default_capability import DefaultManifest
from mux.testing.conformance.runner import run_fixture
from mux.testing.drivers.openai import conformance, conformance_sdk, default_capability, offline_sdk
from mux.testing.drivers.openai.offline_sdk import OfflineAsyncOpenAI

from . import test_interaction_deployment as interaction

SETTINGS = (
    "OPENAI_ADMIN_KEY",
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_CUSTOM_HEADERS",
)
ERROR = "offline SDK adapter refuses inherited OpenAI credential/account settings"
MANIFEST = DefaultManifest(
    name="fixture",
    system="fixture",
    skills=(),
    builtin_tools=(),
    mcp=MCPConnection(name="fixture", url="https://fixture.invalid/mcp"),
)


@pytest.mark.parametrize("setting", SETTINGS)
def test_preflight_reads_only_environment_names(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    class NamesOnly(Mapping[str, str]):
        def __iter__(self) -> Iterator[str]:
            yield setting

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> str:
            pytest.fail("environment values must never be fetched by preflight")

    with monkeypatch.context() as restricted:
        restricted.setattr(offline_sdk.os, "environ", NamesOnly())
        with pytest.raises(ValueError) as failure:
            offline_sdk.refuse_inherited_settings()
        assert str(failure.value) == ERROR


def poison(monkeypatch: pytest.MonkeyPatch, setting: str, empty: bool) -> tuple[str, ...]:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    names = SETTINGS if setting == "all" else (setting,)
    for name in names:
        value = "" if empty else "fixture-poison"
        if name == "OPENAI_CUSTOM_HEADERS" and not empty:
            value = "Authorization: fixture-poison\nX-Unsafe: fixture-poison"
        monkeypatch.setenv(name, value)
    return names


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["C04", "C11", "boundaries", "F1"])
@pytest.mark.parametrize("setting", [*SETTINGS, "all"])
@pytest.mark.parametrize("empty", [False, True])
async def test_factories_refuse_before_script_http_or_sdk_allocation(
    monkeypatch: pytest.MonkeyPatch, entry: str, setting: str, empty: bool
) -> None:
    names = poison(monkeypatch, setting, empty)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("ambient settings must refuse before allocating script, HTTP or SDK")

    monkeypatch.setattr(conformance, "OpenAIScriptedTransport", forbidden)
    monkeypatch.setattr(conformance_sdk, "OpenAISDKScript", forbidden)
    monkeypatch.setattr(default_capability, "OpenAIDefaultCapabilityTransport", forbidden)
    monkeypatch.setattr(interaction, "Wire", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(offline_sdk.AsyncOpenAI, "__init__", forbidden)
    with pytest.raises(ValueError) as failure:
        if entry == "C04":
            conformance.factory()
        elif entry == "C11":
            async with interaction.deployment_adapter():
                pytest.fail("C11 must refuse")
        elif entry == "F1":
            default_capability.OpenAIDefaultCapabilityFactory(MANIFEST)()
        else:
            async with conformance_sdk.offline_sdk_adapter():
                pytest.fail("boundaries must refuse")
    assert str(failure.value) == ERROR
    assert all(name in os.environ for name in names)


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", SETTINGS)
@pytest.mark.parametrize("empty", [False, True])
async def test_c04_late_settings_refuse_before_native_read_or_copy(
    monkeypatch: pytest.MonkeyPatch, setting: str, empty: bool
) -> None:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    async with conformance.OpenAIOfflineFactory() as factory:
        adapter = factory()
        script = adapter.transport
        assert isinstance(script, conformance.OpenAIScriptedTransport)
        names = poison(monkeypatch, setting, empty)
        with pytest.raises(ValueError) as failure:
            await script.arrange("C04")
        assert str(failure.value) == ERROR
        assert script.requests == [] and script.binding is None
        assert script.fixture == ""
        for name in names:
            monkeypatch.delenv(name)
        assert (await run_fixture("C04", adapter)).status == "pass"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["construct", "copy", "with_options", "transport"])
@pytest.mark.parametrize("setting", SETTINGS)
@pytest.mark.parametrize("empty", [False, True])
async def test_all_owned_sdk_clients_refuse_before_parent_construction(
    monkeypatch: pytest.MonkeyPatch, boundary: str, setting: str, empty: bool
) -> None:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
        ) as http,
        OfflineAsyncOpenAI(http_client=http) as sdk,
    ):
        poison(monkeypatch, setting, empty)

        def forbidden(*args: object, **kwargs: object) -> None:
            pytest.fail("SDK copies must refuse before reading inherited settings")

        monkeypatch.setattr(offline_sdk.AsyncOpenAI, "__init__", forbidden)
        with pytest.raises(ValueError) as failure:
            if boundary == "construct":
                OfflineAsyncOpenAI(http_client=http)
            elif boundary == "copy":
                sdk.copy()
            elif boundary == "with_options":
                sdk.with_options(max_retries=0)
            else:
                SDKTransport(sdk)
        assert str(failure.value) == ERROR
        assert seen == []
        assert sdk.admin_api_key == sdk.webhook_secret == sdk.organization == sdk.project == ""


@pytest.mark.asyncio
async def test_real_sdk_copy_has_explicit_offline_auth_routing_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-poison")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://fixture-poison.invalid/v1")
    monkeypatch.setenv("HTTPS_PROXY", "https://fixture-poison.invalid")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
        ) as http,
        OfflineAsyncOpenAI(http_client=http) as sdk,
    ):
        copied = sdk.with_options(max_retries=0)
        assert isinstance(copied, OfflineAsyncOpenAI)
        assert copied.admin_api_key == copied.webhook_secret == ""
        assert copied.organization == copied.project == ""
        await SDKTransport(copied).request("GET", "/agents")
    assert len(seen) == 1
    assert seen[0].url.host == "openai.invalid"
    assert seen[0].headers["authorization"] == "Bearer offline-fixture"
    assert not seen[0].headers.get("OpenAI-Organization")
    assert not seen[0].headers.get("OpenAI-Project")
    assert "fixture-poison" not in str(seen[0].headers)

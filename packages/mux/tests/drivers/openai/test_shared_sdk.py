"""Shared isolated SDK admission through real OpenAI factory and copy paths."""

from collections.abc import Iterator, Mapping

import httpx
import pytest
from mux.drivers.openai.transport import SDKTransport
from mux.testing import isolated_sdk
from mux.testing.drivers.openai import conformance, conformance_sdk, default_capability
from mux.testing.isolated_sdk import SDK_ENV_VARS, build_async_openai
from openai import AsyncOpenAI

from . import test_interaction_deployment as interaction
from .test_offline_sdk import MANIFEST, SETTINGS


async def exercise_factory(entry: str) -> None:
    if entry == "C04":
        conformance.factory(sdk_factory=build_async_openai)
    elif entry == "C11":
        async with interaction.deployment_adapter():
            pytest.fail("poisoned C11 factory must refuse")
    elif entry == "F1":
        default_capability.OpenAIDefaultCapabilityFactory(
            MANIFEST, sdk_factory=build_async_openai
        )()
    else:
        async with conformance_sdk.offline_sdk_adapter(sdk_factory=build_async_openai):
            pytest.fail("poisoned SDK component factory must refuse")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["C04", "C11", "F1", "boundaries"])
@pytest.mark.parametrize("setting", SDK_ENV_VARS)
@pytest.mark.parametrize("value", ["", "fixture-poison"])
async def test_shared_factory_refuses_before_http_or_sdk_construction(
    monkeypatch: pytest.MonkeyPatch, entry: str, setting: str, value: str
) -> None:
    monkeypatch.setenv(setting, value)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("ambient admission must precede HTTP and SDK construction")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(AsyncOpenAI, "__init__", forbidden)
    with pytest.raises(ValueError) as failure:
        await exercise_factory(entry)
    if setting in SETTINGS:
        assert str(failure.value) == (
            "offline SDK adapter refuses inherited OpenAI credential/account settings"
        )
    else:
        assert str(failure.value) == "test SDK refuses ambient variables: " + setting
    assert set(isolated_sdk.os.environ) >= {setting}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["C04", "C11", "F1", "boundaries"])
@pytest.mark.parametrize("setting", SDK_ENV_VARS)
async def test_shared_factory_reads_environment_names_only(
    monkeypatch: pytest.MonkeyPatch, entry: str, setting: str
) -> None:
    class NamesOnly(Mapping[str, str]):
        def __iter__(self) -> Iterator[str]:
            yield setting

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> str:
            pytest.fail("factory preflight must never read environment values")

    with monkeypatch.context() as restricted:
        restricted.setattr(isolated_sdk.os, "environ", NamesOnly())
        with pytest.raises(ValueError):
            await exercise_factory(entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["copy", "with_options", "transport"])
@pytest.mark.parametrize("setting", SDK_ENV_VARS)
@pytest.mark.parametrize("value", ["", "fixture-poison"])
async def test_shared_sdk_copies_refuse_late_environment_before_parent_constructor(
    monkeypatch: pytest.MonkeyPatch, boundary: str, setting: str, value: str
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        monkeypatch.setenv(setting, value)

        def forbidden(*args: object, **kwargs: object) -> None:
            pytest.fail("late ambient admission must precede parent SDK construction")

        monkeypatch.setattr(AsyncOpenAI, "__init__", forbidden)
        with pytest.raises(ValueError) as failure:
            if boundary == "copy":
                sdk.copy()
            elif boundary == "with_options":
                sdk.with_options(max_retries=0)
            else:
                SDKTransport(sdk)
        assert str(failure.value) == "test SDK refuses ambient variables: " + setting
        assert requests == []
        assert sdk.admin_api_key is sdk.webhook_secret is sdk.organization is sdk.project is None


@pytest.mark.asyncio
async def test_shared_sdk_real_transport_has_explicit_auth_and_native_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "https://fixture-poison.invalid")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    with pytest.raises(isolated_sdk.AmbientSDKConfigurationError, match="HTTPS_PROXY"):
        async with build_async_openai(
            transport=httpx.MockTransport(handle),
            api_key="offline-fixture",
            base_url="https://openai.invalid/v1",
        ):
            pytest.fail("ambient proxy must refuse before transport construction")
    assert requests == []
    monkeypatch.delenv("HTTPS_PROXY")

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline-fixture",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        assert sdk.max_retries == 0
        await SDKTransport(sdk).request(
            "POST", "/agents/sessions/s/events", body={"events": []}, key="fixture"
        )
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "openai.invalid"
    assert request.headers["Authorization"] == "Bearer offline-fixture"
    assert request.headers["OpenAI-Beta"] == "agents=v1"
    assert request.headers["Idempotency-Key"] == "fixture"
    assert not request.headers.get("OpenAI-Organization")
    assert not request.headers.get("OpenAI-Project")
    assert "fixture-poison" not in str(request.headers)

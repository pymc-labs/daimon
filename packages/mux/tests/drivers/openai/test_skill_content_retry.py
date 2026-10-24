"""Post-publish version-metadata and content reads tolerate the provider's
brief read-after-write 404 — confirmed live against the QA OpenAI key: both
endpoints 404 immediately after publish, then succeed within seconds."""

import io
import zipfile

import httpx
import pytest
from mux.contracts.errors import ProviderError
from mux.contracts.ids import Scope, SkillRef
from mux.contracts.resources import SkillUpload, SkillUploadFile
from mux.drivers.openai._common import Context
from mux.drivers.openai.skill_bindings import resolve_pins
from mux.drivers.openai.skills import bundle_digest
from mux.drivers.openai.transport import SDKTransport
from mux.testing.isolated_sdk import build_async_openai

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="human", authorization_id="grant"
)
BUNDLE = SkillUpload(files=(SkillUploadFile(path="SKILL.md", content=b"retained"),))


def packed() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in BUNDLE.files:
            archive.writestr("wrapper/" + file.path, file.content)
    return output.getvalue()


async def test_two_transient_404s_on_content_are_retried_and_succeed(monkeypatch):
    """Matches the live probe shape against the QA OpenAI key: 404, 404, 200."""
    monkeypatch.setattr("mux.drivers.openai.skills.asyncio.sleep", _no_sleep)
    content_calls = 0

    def handle(request):
        nonlocal content_calls
        if request.url.path.endswith("/content"):
            content_calls += 1
            if content_calls <= 2:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, content=packed())
        return httpx.Response(200, json={"skill_id": "native", "version": "7"})

    async with build_async_openai(transport=httpx.MockTransport(handle)) as sdk:
        context = Context(
            SDKTransport(sdk),
            "project",
            "openai.persistent_workspace",
            lambda actual, *_: actual == SCOPE,
        )
        pins = (SkillRef(id="native", version="7", digest=bundle_digest(BUNDLE)),)
        assert await resolve_pins(pins, SCOPE, context) == pins
    assert content_calls == 3


async def test_two_transient_404s_on_version_metadata_are_retried_and_succeed(monkeypatch):
    """The version-metadata GET 404s on the same post-publish window as content —
    reproduced live against the QA OpenAI key independently of the content path."""
    monkeypatch.setattr("mux.drivers.openai.skills.asyncio.sleep", _no_sleep)
    metadata_calls = 0

    def handle(request):
        nonlocal metadata_calls
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=packed())
        metadata_calls += 1
        if metadata_calls <= 2:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(200, json={"skill_id": "native", "version": "7"})

    async with build_async_openai(transport=httpx.MockTransport(handle)) as sdk:
        context = Context(
            SDKTransport(sdk),
            "project",
            "openai.persistent_workspace",
            lambda actual, *_: actual == SCOPE,
        )
        pins = (SkillRef(id="native", version="7", digest=bundle_digest(BUNDLE)),)
        assert await resolve_pins(pins, SCOPE, context) == pins
    assert metadata_calls == 3


async def test_persistent_404_on_content_still_raises(monkeypatch):
    monkeypatch.setattr("mux.drivers.openai.skills.asyncio.sleep", _no_sleep)
    content_calls = 0

    def handle(request):
        nonlocal content_calls
        if request.url.path.endswith("/content"):
            content_calls += 1
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(200, json={"skill_id": "native", "version": "7"})

    async with build_async_openai(transport=httpx.MockTransport(handle)) as sdk:
        context = Context(
            SDKTransport(sdk),
            "project",
            "openai.persistent_workspace",
            lambda actual, *_: actual == SCOPE,
        )
        pins = (SkillRef(id="native", version="7", digest=bundle_digest(BUNDLE)),)
        with pytest.raises(ProviderError) as excinfo:
            await resolve_pins(pins, SCOPE, context)
    assert excinfo.value.category == "not_found"
    # 5 retries (0.5, 1, 2, 4, 8s) + the final unguarded attempt.
    assert content_calls == 6


async def test_persistent_404_on_version_metadata_still_raises(monkeypatch):
    monkeypatch.setattr("mux.drivers.openai.skills.asyncio.sleep", _no_sleep)
    metadata_calls = 0

    def handle(request):
        nonlocal metadata_calls
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=packed())
        metadata_calls += 1
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with build_async_openai(transport=httpx.MockTransport(handle)) as sdk:
        context = Context(
            SDKTransport(sdk),
            "project",
            "openai.persistent_workspace",
            lambda actual, *_: actual == SCOPE,
        )
        pins = (SkillRef(id="native", version="7", digest=bundle_digest(BUNDLE)),)
        with pytest.raises(ProviderError) as excinfo:
            await resolve_pins(pins, SCOPE, context)
    assert excinfo.value.category == "not_found"
    # 5 retries (0.5, 1, 2, 4, 8s) + the final unguarded attempt.
    assert metadata_calls == 6


async def _no_sleep(_delay: float) -> None:
    return None

"""Self-hosted output files through the real driver against an in-process harness."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from mux.contracts.ids import PageRequest
from mux.drivers.selfhosted import artifacts, wire
from mux.drivers.selfhosted.artifacts import artifact_identity, artifact_route
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability

from .test_driver import SCOPE, Harness, setup

PAGE = PageRequest()


class OutputHarness(Harness):
    """The C3 fake plus the 0.2.0 output routes over an in-memory workspace."""

    def __init__(self, *, legacy: bool = False, foundry: bool = False) -> None:
        super().__init__(foundry=foundry)
        self.legacy = legacy
        self.files: dict[str, bytes] = {}
        self.output_calls: list[tuple[str, str, dict[str, str]]] = []
        self.content_override: dict[str, str | int] | None = None
        self.session_missing = False

    def ids(self) -> dict[str, str]:
        return {wire.output_id(path, f"1-{len(data)}-1"): path for path, data in self.files.items()}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if "/outputs" not in request.url.path and b"/outputs" not in request.content:
            return super().handle(request)
        method, path, query = request.method, request.url.path, dict(request.url.params)
        if self.foundry:
            envelope = json.loads(request.content)["c3"]
            method, path, query = envelope["method"], envelope["path"], envelope["query"]
        self.output_calls.append((method, path, {k: str(v) for k, v in query.items()}))
        if self.legacy:
            return httpx.Response(404, json={"detail": "Not Found"})
        if self.session_missing:
            return httpx.Response(404, json={"detail": "session_not_found"})
        tail = path.split("/outputs", 1)[1].strip("/").split("/")
        if tail == [""]:
            outputs = [
                {"id": key, "path": name, "size_bytes": len(self.files[name]), "modified_at": NOW}
                for key, name in self.ids().items()
            ]
            return httpx.Response(200, json={"outputs": outputs, "truncated": False})
        name = self.ids().get(tail[0])
        if name is None:
            return httpx.Response(404, json={"detail": "output_not_found"})
        if tail[1:] == ["delete"]:
            del self.files[name]
            return httpx.Response(200, json={"deleted": True, "id": tail[0]})
        data = self.files[name]
        body = self.content_override or {
            "id": tail[0],
            "size_bytes": len(data),
            "content_b64": base64.b64encode(data).decode(),
        }
        return httpx.Response(200, json=body)


NOW = datetime(2026, 1, 1, tzinfo=UTC).isoformat()


@pytest.mark.parametrize("foundry", [False, True])
async def test_outputs_when_saved_then_listed_downloaded_and_deleted(foundry: bool) -> None:
    """A saved file is listed as an artifact, downloads intact and deletes on the harness."""
    harness = OutputHarness(foundry=foundry)
    harness.files = {"reports/summary.csv": b"a,b\n"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        driver, session = await setup(client, harness)
        [artifact] = (await driver.artifacts.list(SCOPE, session.ref, page=PAGE)).data
        assert (artifact.filename, artifact.media_type, artifact.size_bytes) == (
            "summary.csv",
            "text/csv",
            4,
        ), artifact
        assert artifact.session == session.ref, "the artifact must belong to its session"
        retrieved = await driver.artifacts.retrieve(SCOPE, artifact.ref)
        assert retrieved == artifact, "retrieve must find the listed file"
        data = b"".join([chunk async for chunk in driver.artifacts.download(SCOPE, artifact.ref)])
        assert data == b"a,b\n", "download must return the saved bytes"
        receipt = await driver.artifacts.delete(SCOPE, artifact.ref, key="del")
        assert receipt.deleted == (artifact.ref,), receipt
    assert harness.files == {}, "delete must remove the file on the harness"
    assert all(q.get("session_id") == session.ref.id for _, _, q in harness.output_calls), (
        "every output call must carry the session id"
    )


async def test_outputs_when_harness_predates_outputs_then_empty_and_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An older harness without output routes delivers nothing and never fails the reply."""
    harness = OutputHarness(legacy=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        driver, session = await setup(client, harness)
        with caplog.at_level(logging.WARNING, logger="mux.drivers.selfhosted.artifacts"):
            for _ in range(3):
                page = await driver.artifacts.list(SCOPE, session.ref, page=PAGE)
                assert page.data == (), "an old harness must list nothing"
    warnings = [r for r in caplog.records if r.message == "selfhosted.outputs_unavailable"]
    assert len(warnings) == 1, "the missing routes must be logged once"
    assert len(harness.output_calls) == 1, "later sweeps must not ask the old harness again"


async def test_outputs_when_old_harness_upgraded_then_reprobed_after_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness upgraded in place delivers again once the wait has passed."""
    harness = OutputHarness(legacy=True)
    harness.files = {"a.txt": b"hi"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        driver, session = await setup(client, harness)
        assert (await driver.artifacts.list(SCOPE, session.ref, page=PAGE)).data == (), "old"
        harness.legacy = False
        monkeypatch.setattr(artifacts.time, "monotonic", lambda: 1e12)
        page = await driver.artifacts.list(SCOPE, session.ref, page=PAGE)
    assert [a.filename for a in page.data] == ["a.txt"], "the upgraded harness must be asked again"


async def test_outputs_when_session_missing_then_empty_and_not_latched() -> None:
    """A 404 for one session is not a missing route: the next sweep asks again."""
    harness = OutputHarness()
    harness.session_missing = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        driver, session = await setup(client, harness)
        for _ in range(2):
            page = await driver.artifacts.list(SCOPE, session.ref, page=PAGE)
            assert page.data == (), "a missing session lists nothing"
    assert len(harness.output_calls) == 2, "a missing session must not stop later sweeps"


async def test_outputs_when_content_is_malformed_then_refused() -> None:
    """A read whose id, size or encoding disagrees with the request is refused."""
    harness = OutputHarness()
    harness.files = {"a.txt": b"hi"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        driver, session = await setup(client, harness)
        [artifact] = (await driver.artifacts.list(SCOPE, session.ref, page=PAGE)).data
        [key] = harness.ids()
        for override in (
            {"id": "other", "size_bytes": 2, "content_b64": "aGk="},
            {"id": key, "size_bytes": 2, "content_b64": "not base64!"},
        ):
            harness.content_override = override
            with pytest.raises(ProviderError) as error:
                _ = [c async for c in driver.artifacts.download(SCOPE, artifact.ref)]
            assert error.value.native_code == "malformed_output", override
        harness.content_override = {"id": key, "size_bytes": 3, "content_b64": "aGk="}
        with pytest.raises(ProviderError) as error:
            _ = [c async for c in driver.artifacts.download(SCOPE, artifact.ref)]
        assert error.value.retryable, "a short read must be retryable"


async def test_outputs_when_upload_or_turn_filter_then_unsupported() -> None:
    """Daimon never uploads files to the harness, and listings are per session only."""
    harness = OutputHarness()
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        driver, session = await setup(client, harness)

        async def body():
            yield b"x"

        with pytest.raises(UnsupportedCapability):
            await driver.artifacts.upload(
                SCOPE, body(), filename="a", media_type="text/plain", key="k"
            )
        with pytest.raises(UnsupportedCapability):
            await driver.artifacts.list(SCOPE, session.ref, page=PAGE, turn_id="turn")


def test_output_ids_when_noncanonical_or_escaping_then_rejected() -> None:
    """Ids round-trip, and any other encoding or escaping path is refused."""
    key = wire.output_id("dir/a.txt", "1-2-3")
    assert wire.output_route(key) == ("dir/a.txt", "1-2-3"), "an id must round-trip"
    assert artifact_route(artifact_identity("s", key)) == ("s", key), "a ref must round-trip"
    for bad in (key + "=", wire.output_id("../a", "v"), wire.output_id("/etc/x", "v"), "!!"):
        with pytest.raises(ValueError):
            wire.output_route(bad)
    with pytest.raises(ValueError):
        artifact_route(artifact_identity("s", key) + "=")


async def test_outputs_when_ref_is_forged_or_foreign_then_refused_before_harness() -> None:
    """A ref from another account, or not the one listed, never reaches the harness."""
    harness = OutputHarness()
    harness.files = {"a.txt": b"hi"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)) as client:
        driver, session = await setup(client, harness)
        [artifact] = (await driver.artifacts.list(SCOPE, session.ref, page=PAGE)).data
        calls = len(harness.output_calls)
        for ref in (
            artifact.ref.model_copy(update={"account_id": "other"}),
            artifact.ref.model_copy(update={"kind": "session"}),
            artifact.ref.model_copy(update={"id": "bm90IGFuIGlk"}),
        ):
            with pytest.raises(ScopeViolation):
                await driver.artifacts.delete(SCOPE, ref, key="del")
        other = SCOPE.model_copy(update={"account_id": "other"})
        with pytest.raises(ScopeViolation):
            await driver.artifacts.delete(other, artifact.ref, key="del")
    assert len(harness.output_calls) == calls, "a refused ref must not call the harness"
    assert harness.files, "a refused delete must keep the file"

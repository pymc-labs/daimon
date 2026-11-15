"""Offline scripted transport and storage, explicitly test-only; never SDK discovery."""

import asyncio
import io
import tarfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy

from pydantic import JsonValue

from mux.drivers.gemini.storage import Records
from mux.drivers.gemini.transport import Object, OwnedStream
from mux.errors import ProviderError


class MemoryStorage:
    """Test-only transactional storage; retained data survives a driver restart."""

    def __init__(self) -> None:
        self._records = Records()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self, *, after_dispatch: bool = False) -> AsyncIterator[Records]:
        async with self._lock:
            snapshot = deepcopy(self._records)
            yield snapshot
            self._records = snapshot

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Records]:
        """The last committed records, without waiting for a writer; never saved."""
        yield deepcopy(self._records)


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[Object] = []
        self.responses: list[Object | ProviderError] = []
        self.saved: dict[str, Object] = {}
        self.reads: dict[str, list[Object | ProviderError]] = {}
        self.read_requests: list[str] = []
        self.streams: dict[str, list[Object]] = {}
        self.cancelled: list[str] = []
        self.stream_closed = False
        self.snapshots: dict[str, bytes | ProviderError] = {}
        self.snapshot_reads: list[str] = []
        # Environments made before any interaction: config and files (path -> size).
        self.environments: dict[str, Object] = {}
        self.environment_files: dict[str, dict[str, int]] = {}
        self.uploads: list[tuple[str, str, bool]] = []
        self.upload_errors: list[ProviderError] = []
        self.deleted_environments: list[str] = []

    async def create_environment(self, config: Mapping[str, JsonValue]) -> str:
        id_ = f"environment-{len(self.environments) + 1}"
        self.environments[id_] = deepcopy(dict(config))
        self.environment_files[id_] = {}
        return id_

    async def upload_archive(
        self, environment_id: str, path: str, archive: bytes, *, overwrite: bool
    ) -> None:
        self.uploads.append((environment_id, path, overwrite))
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        files = self.environment_files.get(environment_id)
        if files is None:
            raise ProviderError("not_found", retryable=False)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            new = {f"{path}/{m.name}": m.size for m in tar.getmembers() if m.isfile()}
        if not overwrite and set(new) & set(files):
            raise ProviderError("conflict", retryable=False, native_error_code="http_409")
        files.update(new)

    async def list_files(self, environment_id: str, path: str) -> dict[str, int]:
        files = self.environment_files.get(environment_id)
        if files is None:
            raise ProviderError("not_found", retryable=False)
        return {k.removeprefix(path + "/"): v for k, v in files.items() if k.startswith(path + "/")}

    async def delete_environment(self, environment_id: str) -> None:
        self.deleted_environments.append(environment_id)
        if self.environment_files.pop(environment_id, None) is None:
            raise ProviderError("not_found", retryable=False)

    async def download_snapshot(self, environment_id: str) -> bytes:
        self.snapshot_reads.append(environment_id)
        response = self.snapshots.get(environment_id)
        if response is None:
            raise AssertionError("unscripted workspace snapshot read")
        if isinstance(response, ProviderError):
            raise response
        return response

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        self.requests.append(deepcopy(dict(request)))
        if not self.responses:
            raise AssertionError("unscripted provider mutation")
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        self.saved[str(response["id"])] = deepcopy(response)
        return deepcopy(response)

    async def get(self, interaction_id: str) -> Object:
        self.read_requests.append(interaction_id)
        queued = self.reads.get(interaction_id)
        if queued:
            response = queued.pop(0)
            if isinstance(response, ProviderError):
                raise response
            self.saved[interaction_id] = deepcopy(response)
        if interaction_id not in self.saved:
            raise ProviderError("not_found", retryable=False)
        return deepcopy(self.saved[interaction_id])

    async def cancel(self, interaction_id: str) -> Object:
        self.cancelled.append(interaction_id)
        return await self.get(interaction_id)

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        async def events() -> AsyncIterator[Object]:
            try:
                skipping = after is not None
                for raw in self.streams.get(interaction_id, []):
                    if skipping:
                        if raw.get("event_id") == after:
                            skipping = False
                        continue
                    yield deepcopy(raw)
            finally:
                self.stream_closed = True

        async def close() -> None:
            self.stream_closed = True

        return OwnedStream(events(), close)

"""Run a provider turn's preparation inside its key's gate (`daimon.core.provider_key_gate`)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from typing import IO

import structlog
from daimon.core.provider_key_gate import ProviderKey, ProviderKeyGate
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest
from mux.contracts.errors import ProviderError
from mux.drivers.openai import Method, Object, Transport

log = structlog.get_logger(__name__)


async def gated_turn(
    request: ProviderPreparationRequest,
    key: ProviderKey | None,
    prepare: Callable[[], Awaitable[PreparedTurn]],
) -> PreparedTurn:
    """Refuse a cooling key, hold a lease on it until the turn ends, then prepare.

    A bundle with no gate, a runtime with no key and a revision with no model run ungated.
    """
    gate, revision = request.deps.provider_gate, request.admission.backend_revision
    if gate is None or key is None or revision is None or revision.model is None:
        return await prepare()
    if await gate.cooling_until(key, revision.model) is not None:
        raise AdmissionDenied(
            reason="backend_unsupported", cause="provider_cooling", provider=key.provider
        )
    lease = await gate.acquire(key, request.tenant_id)
    if lease is None:
        raise AdmissionDenied(
            reason="backend_unsupported", cause="provider_busy", provider=key.provider
        )
    try:
        prepared = await prepare()
    except BaseException:
        await asyncio.shield(gate.release(lease))
        raise
    inner = prepared._finalize  # pyright: ignore[reportPrivateUsage]

    async def finalize() -> None:
        try:
            if inner is not None:
                await inner()
        finally:
            await gate.release(lease)

    return replace(prepared, _finalize=finalize)


class CooldownTransport:
    """OpenAI's transport, noting each rate limit the provider reports for the key's model."""

    def __init__(self, inner: Transport, gate: ProviderKeyGate, key: ProviderKey, model: str):
        self._inner, self._gate, self._key, self._model = inner, gate, key, model

    async def _noting[T](self, call: Awaitable[T]) -> T:
        try:
            return await call
        except ProviderError as error:
            if error.category == "rate_limited" and error.retry_after_s is not None:
                try:
                    await self._gate.record_cooldown(self._key, self._model, error.retry_after_s)
                except Exception:
                    log.warning("provider_key_gate.cooldown_failed", exc_info=True)
            raise

    async def request(
        self,
        method: Method,
        path: str,
        *,
        body: Object | None = None,
        query: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> Object:
        return await self._noting(
            self._inner.request(method, path, body=body, query=query, key=key)
        )

    async def open_stream(self, path: str) -> AsyncIterator[Object]:
        return await self._noting(self._inner.open_stream(path))

    async def multipart(
        self,
        path: str,
        *,
        files: tuple[tuple[str, str, bytes | IO[bytes], str], ...],
        fields: Mapping[str, str],
        key: str,
    ) -> Object:
        return await self._noting(self._inner.multipart(path, files=files, fields=fields, key=key))

    async def download(self, path: str) -> AsyncIterator[bytes]:
        return await self._inner.download(path)

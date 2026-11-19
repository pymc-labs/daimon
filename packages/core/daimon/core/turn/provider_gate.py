"""Run a provider turn's preparation inside its key's gate (`daimon.core.provider_key_gate`)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import replace
from typing import IO

import structlog
from daimon.core.channel_backend import backend_refusal_causes
from daimon.core.provider_key_gate import (
    RENEW_EVERY_S,
    ProviderKey,
    ProviderKeyGate,
    ProviderKeySource,
)
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest
from mux.contracts.errors import ProviderError
from mux.drivers.openai import Method, Object, Transport

log = structlog.get_logger(__name__)


class GateHold:
    """One turn's place in its key's gate, taken once its policy checks have passed.

    Preparation calls `enter` after every refusal that needs no provider key, so a
    denied turn never reads the key. The lease is short and renewed until the turn's
    finalizer releases it or the task that took it ends, whichever exit it takes
    between preparation and the run; a crashed process's lease expires on its own.
    """

    def __init__(self, request: ProviderPreparationRequest) -> None:
        self._request = request
        self._keeper: asyncio.Task[None] | None = None
        self._ended = asyncio.Event()

    @property
    def held(self) -> bool:
        return self._keeper is not None

    async def enter(self, source: ProviderKeySource | None) -> None:
        """Refuse a cooling or full key; else take a lease on it.

        A bundle with no gate, a runtime with no key and a revision with no model run ungated.
        """
        gate, revision = self._request.deps.provider_gate, self._request.admission.backend_revision
        key = source() if source is not None else None
        if gate is None or key is None or revision is None or revision.model is None:
            return
        if await gate.cooling_until(key, revision.model) is not None:
            raise AdmissionDenied(
                reason="backend_unsupported", cause="provider_cooling", provider=key.provider
            )
        lease = await gate.acquire(key, self._request.tenant_id)
        if lease is None:
            raise AdmissionDenied(
                reason="backend_unsupported", cause="provider_busy", provider=key.provider
            )
        # Past the turn's ceiling nothing is left to hold the slot for.
        lasts = (self._request.deadline - self._request.now()).total_seconds() + 60
        ended = self._ended
        self._keeper = asyncio.create_task(_keep(gate, lease, lasts, ended))
        owner = asyncio.current_task()
        if owner is not None:
            owner.add_done_callback(lambda _: ended.set())

    async def release(self) -> None:
        keeper, self._keeper = self._keeper, None
        if keeper is not None:
            self._ended.set()
            # Not `await keeper`: a cancelled caller must not cancel the release.
            await asyncio.wait([keeper])


async def _keep(
    gate: ProviderKeyGate, lease: uuid.UUID, seconds: float, ended: asyncio.Event
) -> None:
    """Renew `lease` until `ended` is set or `seconds` pass, then free it."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    try:
        while (left := end - loop.time()) > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ended.wait(), min(RENEW_EVERY_S, left))
            if ended.is_set():
                return
            await gate.renew(lease)
    finally:
        await gate.release(lease)


async def gated_turn(
    request: ProviderPreparationRequest,
    prepare: Callable[[ProviderPreparationRequest], Awaitable[PreparedTurn]],
) -> PreparedTurn:
    """Prepare a turn; the lease its `request.gate.enter` takes lasts until the turn ends."""
    hold = GateHold(request)
    try:
        prepared = await prepare(replace(request, gate=hold))
    except BaseException:
        await asyncio.shield(hold.release())
        raise
    if not hold.held:
        return prepared
    inner = prepared._finalize  # pyright: ignore[reportPrivateUsage]

    async def finalize() -> None:
        try:
            if inner is not None:
                await inner()
        finally:
            await hold.release()

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
                    log.warning(
                        "provider_key_gate.cooldown_failed",
                        causes=backend_refusal_causes(error),
                        exc_info=True,
                    )
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

"""Explicit host Flash policy; no client creation, retry or implicit selection."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import JsonValue

from mux.contracts.ids import ModelRef
from mux.drivers.gemini.transport import Object, Transport, object_value, string
from mux.errors import ProviderError

FLASH_PRIMARY = "gemini-3.8-flash"
FLASH_MODELS = (FLASH_PRIMARY, "gemini-flash-latest", "gemini-3.5-flash-lite")


@dataclass(frozen=True)
class FlashAttempt:
    model: ModelRef
    attempt: int
    outcome: Literal["accepted", "refused", "unknown"]
    http_status: int | None = None
    interaction_id: str | None = None


class FlashAccounting(Protocol):
    """Caller persists a hold before each dispatch, and preserves its outcome.

    A failed accounting operation stops the chain. This policy cannot waive a
    budget or infer a free call from a refusal. Actuals arrive via scoped Usage.
    """

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None: ...
    async def after_attempt(self, *, receipt: FlashAttempt) -> None: ...


class FlashTransport:
    def __init__(self, source: Transport, accounting: FlashAccounting) -> None:
        self.source, self.accounting = source, accounting
        self._models: dict[str, ModelRef] = {}

    def model_for_interaction(self, interaction: str) -> ModelRef | None:
        return self._models.get(interaction)

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        original = object_value(request["agent_config"])
        if original.get("model") != FLASH_PRIMARY:
            raise ProviderError(
                "invalid_request", retryable=False, native_code="flash_primary_required"
            )
        for index, name in enumerate(FLASH_MODELS):
            model = ModelRef(provider="gemini", id=name)
            await self.accounting.before_attempt(model=model, attempt=index + 1)
            bounded = dict(request)
            bounded["agent_config"] = {**original, "model": name}
            try:
                reply = await self.source.create(bounded)
            except ProviderError as error:
                refused = error.category == "overloaded" and error.native_code == "503"
                await self.accounting.after_attempt(
                    receipt=FlashAttempt(
                        model=model,
                        attempt=index + 1,
                        outcome="refused" if refused else "unknown",
                        http_status=503 if refused else None,
                    )
                )
                if not refused or index + 1 == len(FLASH_MODELS):
                    raise
                continue
            # No fallback on malformed/lost acceptance or persistence failure.
            interaction = string(reply["id"])
            string(reply["status"])
            self._models[interaction] = model
            await self.accounting.after_attempt(
                receipt=FlashAttempt(
                    model=model,
                    attempt=index + 1,
                    outcome="accepted",
                    interaction_id=interaction,
                )
            )
            return reply
        raise ProviderError("upstream", retryable=False, native_code="flash_chain_exhausted")

    async def get(self, interaction_id: str) -> Object:
        return await self.source.get(interaction_id)

    async def cancel(self, interaction_id: str) -> Object:
        return await self.source.cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        return await self.source.download_snapshot(environment_id)

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        return await self.source.open_stream(interaction_id, after=after)

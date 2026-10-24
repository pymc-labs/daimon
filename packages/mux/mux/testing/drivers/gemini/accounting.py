"""Recording accounting hook for `FlashTransport` tests."""

from __future__ import annotations

from mux.contracts.ids import ModelRef
from mux.drivers.gemini.flash import FlashAttempt


class Accounting:
    def __init__(self) -> None:
        self.held: list[ModelRef] = []
        self.receipts: list[FlashAttempt] = []

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        assert attempt == len(self.held) + 1
        self.held.append(model)

    async def after_attempt(self, *, receipt: FlashAttempt) -> None:
        assert receipt.model == self.held[-1]
        self.receipts.append(receipt)

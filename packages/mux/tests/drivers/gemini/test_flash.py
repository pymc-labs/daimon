"""Host model policy: a create HTTP503 alone permits the next Flash model."""

from collections.abc import Mapping

import pytest
from mux.contracts.ids import ModelRef
from mux.drivers.gemini.fake import FakeTransport
from mux.drivers.gemini.flash import FLASH_MODELS, FlashAttempt, FlashTransport
from mux.drivers.gemini.transport import object_value
from mux.errors import ProviderError
from pydantic import JsonValue


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


def request() -> Mapping[str, JsonValue]:
    return {"agent_config": {"type": "antigravity", "model": FLASH_MODELS[0]}, "input": []}


async def test_503_only_chain_accounts_each_attempt_and_preserves_selected_model() -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.extend(
        [
            ProviderError("overloaded", retryable=True, native_code="503"),
            ProviderError("overloaded", retryable=True, native_code="503"),
            {"id": "interaction", "status": "completed"},
        ]
    )
    policy = FlashTransport(source, accounting)
    await policy.create(request())
    assert [object_value(r["agent_config"])["model"] for r in source.requests] == list(FLASH_MODELS)
    assert [r.outcome for r in accounting.receipts] == ["refused", "refused", "accepted"]
    assert [r.http_status for r in accounting.receipts] == [503, 503, None]
    assert policy.model_for_interaction("interaction") == ModelRef(
        provider="gemini", id=FLASH_MODELS[2]
    )
    assert object_value(request()["agent_config"])["model"] == FLASH_MODELS[0]


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("overloaded", retryable=True, native_code="429"),
        ProviderError("transient_network", retryable=True),
        ProviderError("permission", retryable=False, native_code="403"),
        ProviderError("upstream", retryable=True, native_code="503"),
    ],
)
async def test_other_failures_never_fallback(error: ProviderError) -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.extend([error, {"id": "unexpected", "status": "completed"}])
    with pytest.raises(ProviderError):
        await FlashTransport(source, accounting).create(request())
    assert len(source.requests) == len(accounting.held) == 1
    assert accounting.receipts[0].outcome == "unknown"


async def test_read_503_and_cancel_passthrough_never_create_a_fallback() -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.append({"id": "native", "status": "in_progress"})
    policy = FlashTransport(source, accounting)
    await policy.create(request())
    source.reads["native"] = [ProviderError("overloaded", retryable=True, native_code="503")]
    with pytest.raises(ProviderError):
        await policy.get("native")
    await policy.cancel("native")
    assert source.cancelled == ["native"] and len(source.requests) == 1
    assert len(accounting.receipts) == 1


async def test_unselected_model_refuses_before_accounting_or_provider() -> None:
    source, accounting = FakeTransport(), Accounting()
    with pytest.raises(ProviderError):
        await FlashTransport(source, accounting).create({"agent_config": {"model": "gemini-pro"}})
    assert not source.requests and not accounting.held

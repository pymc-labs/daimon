"""Host model policy: a create HTTP503 alone permits the next Flash model."""

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from mux.contracts.ids import ModelRef, ResourceRef, Revision
from mux.contracts.resources import Agent, AgentSpec
from mux.drivers.gemini.flash import FLASH_MODELS, FLASH_PRIMARY, FlashTransport
from mux.drivers.gemini.transport import object_value
from mux.errors import ProviderError
from mux.profiles.gemini import ARGON_MODEL, WORKER_MAX_TOTAL_TOKENS, WORKER_MODEL
from mux.testing.drivers.gemini.accounting import Accounting
from mux.testing.drivers.gemini.fake import FakeTransport
from pydantic import JsonValue


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


@pytest.mark.parametrize("overloaded", (False, True))
async def test_named_cheap_worker_preserves_budget_and_never_falls_back(overloaded: bool) -> None:
    source, accounting = FakeTransport(), Accounting()
    agent = Agent(
        ref=ResourceRef(
            id="owned-worker", kind="agent", provider="gemini", account_scope_id="project"
        ),
        revision=Revision(local=0),
        spec=AgentSpec(name="worker", model=ModelRef(provider="gemini", id=WORKER_MODEL)),
        created_at=datetime.now(UTC),
    )
    selected: Mapping[str, JsonValue] = {
        "agent": agent.ref.id,
        "agent_config": {
            "type": "antigravity",
            "model": WORKER_MODEL,
            "max_total_tokens": WORKER_MAX_TOTAL_TOKENS,
        },
        "tools": [],
    }
    policy = FlashTransport(source, accounting, native_agent=agent)
    if overloaded:
        source.responses.append(ProviderError("overloaded", retryable=True, native_code="503"))
        with pytest.raises(ProviderError):
            await policy.create(selected)
    else:
        source.responses.append({"id": "owned-interaction", "status": "completed"})
        await policy.create(selected)
    assert source.requests == [selected]
    assert accounting.held == [agent.spec.model]
    assert len(accounting.receipts) == 1


async def test_model_output_cap_survives_the_existing_503_fallback_chain() -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.extend(
        [
            ProviderError("overloaded", retryable=True, native_code="503"),
            ProviderError("overloaded", retryable=True, native_code="503"),
            {"id": "bounded", "status": "completed"},
        ]
    )
    body: Mapping[str, JsonValue] = {
        "model": FLASH_MODELS[0],
        "generation_config": {"max_output_tokens": 4096},
        "tools": [],
    }
    await FlashTransport(source, accounting).create(body)
    assert [call["model"] for call in source.requests] == list(FLASH_MODELS)
    assert all(call["generation_config"] == {"max_output_tokens": 4096} for call in source.requests)
    assert all(
        not {"agent", "agent_config", "environment"} & call.keys() for call in source.requests
    )
    assert len(accounting.held) == 3 and body["model"] == FLASH_MODELS[0]


@pytest.mark.parametrize("budget", (None, False, 0, 65_537))
async def test_model_without_a_valid_output_cap_refuses_before_a_hold(budget) -> None:
    from mux.errors import UnsupportedCapability

    source, accounting = FakeTransport(), Accounting()
    with pytest.raises(UnsupportedCapability):
        await FlashTransport(source, accounting).create(
            {
                "model": FLASH_MODELS[0],
                "generation_config": {"max_output_tokens": budget},
                "tools": [],
            }
        )
    assert source.requests == [] and accounting.held == []


async def test_output_cap_is_snapshotted_before_the_accounting_await() -> None:
    body: dict[str, JsonValue] = {
        "model": FLASH_MODELS[0],
        "generation_config": {"max_output_tokens": 4096},
        "tools": [],
    }

    class MutatingAccounting(Accounting):
        async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
            await super().before_attempt(model=model, attempt=attempt)
            body["generation_config"] = {"max_output_tokens": 8192}

    source, accounting = FakeTransport(), MutatingAccounting()
    source.responses.append({"id": "capped", "status": "completed"})
    await FlashTransport(source, accounting).create(body)
    assert source.requests[0]["generation_config"] == {"max_output_tokens": 4096}


async def test_bounded_worker_pins_counted_model_and_never_falls_back():
    from .test_core import CountingTransport

    source, accounting = CountingTransport(262144), Accounting()
    transport = FlashTransport(source, accounting, bounded_worker=True)
    body = {
        "model": FLASH_PRIMARY,
        "tools": [],
        "generation_config": {"max_output_tokens": 65536},
        "input": [{"type": "text", "text": "task"}],
    }
    assert await transport.count_input_tokens(body) == 262144
    source.responses.append(ProviderError("overloaded", retryable=True, native_code="503"))
    with pytest.raises(ProviderError):
        await transport.create(body)
    assert len(source.requests) == len(accounting.held) == 1
    assert accounting.held[0].id == FLASH_PRIMARY


def argon_request() -> Mapping[str, JsonValue]:
    return {"agent_config": {"type": "antigravity", "model": ARGON_MODEL}, "input": []}


@pytest.mark.parametrize("overloaded", (False, True))
async def test_argon_primary_is_one_attempt_and_never_falls_back_to_flash(
    overloaded: bool,
) -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.append(
        ProviderError("overloaded", retryable=True, native_code="503")
        if overloaded
        else {"id": "argon", "status": "completed"}
    )
    source.responses.append({"id": "flash", "status": "completed"})
    policy = FlashTransport(source, accounting, primary=ARGON_MODEL)
    if overloaded:
        with pytest.raises(ProviderError):
            await policy.create(argon_request())
        assert accounting.receipts[0].outcome == "refused"
    else:
        await policy.create(argon_request())
        assert policy.model_for_interaction("argon") == ModelRef(provider="gemini", id=ARGON_MODEL)
    assert source.requests == [argon_request()]
    assert accounting.held == [ModelRef(provider="gemini", id=ARGON_MODEL)]


async def test_argon_and_flash_primaries_refuse_each_others_requests() -> None:
    source, accounting = FakeTransport(), Accounting()
    with pytest.raises(ProviderError, match="flash_primary_required"):
        await FlashTransport(source, accounting, primary=ARGON_MODEL).create(request())
    with pytest.raises(ProviderError, match="flash_primary_required"):
        await FlashTransport(source, accounting).create(argon_request())
    assert not source.requests and not accounting.held


@pytest.mark.parametrize("primary", (WORKER_MODEL, "gemini-4-argon-preview", "gemini-pro"))
def test_transport_primary_is_flash_or_argon_only(primary: str) -> None:
    with pytest.raises(ValueError):
        FlashTransport(FakeTransport(), Accounting(), primary=primary)


@pytest.mark.parametrize("overloaded", (False, True))
async def test_bounded_argon_worker_runs_argon_only_and_never_falls_back(overloaded: bool) -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.append(
        ProviderError("overloaded", retryable=True, native_code="503")
        if overloaded
        else {"id": "argon", "status": "in_progress"}
    )
    capped: Mapping[str, JsonValue] = {
        "model": ARGON_MODEL,
        "tools": [],
        "generation_config": {"max_output_tokens": 4096},
        "input": [{"type": "text", "text": "task"}],
    }
    policy = FlashTransport(source, accounting, bounded_worker=True, primary=ARGON_MODEL)
    if overloaded:
        with pytest.raises(ProviderError):
            await policy.create(capped)
    else:
        await policy.create(capped)
    assert [r["model"] for r in source.requests] == [ARGON_MODEL]
    assert accounting.held == [ModelRef(provider="gemini", id=ARGON_MODEL)]


async def test_bounded_worker_refuses_an_uncapped_argon_request() -> None:
    source, accounting = FakeTransport(), Accounting()
    uncapped: Mapping[str, JsonValue] = {
        "agent_config": {"type": "antigravity", "model": ARGON_MODEL},
        "input": [],
    }
    with pytest.raises(ProviderError):
        await FlashTransport(source, accounting, bounded_worker=True, primary=ARGON_MODEL).create(
            uncapped
        )
    assert not source.requests and not accounting.held


def http(category: str, status: int, *, code: str | None = None) -> ProviderError:
    """What `transport.normalize_error` yields for an HTTP status on create."""
    return ProviderError(
        category,  # type: ignore[arg-type]
        retryable=status == 429 or status >= 500,
        native_code=code,
        native_error_code=f"http_{status}",
    )


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (http("invalid_request", 400), 400),
        (http("auth", 401), 401),
        (http("permission", 403), 403),
        (http("not_found", 404), 404),
        (http("invalid_request", 422), 422),
    ],
)
async def test_definite_4xx_on_create_is_a_refusal_that_never_falls_back(
    error: ProviderError, status: int
) -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.extend([error, {"id": "unexpected", "status": "completed"}])
    with pytest.raises(ProviderError):
        await FlashTransport(source, accounting).create(request())
    assert len(source.requests) == len(accounting.held) == 1
    (receipt,) = accounting.receipts
    assert (receipt.outcome, receipt.http_status, receipt.interaction_id) == (
        "refused",
        status,
        None,
    )


@pytest.mark.parametrize(
    "error",
    [
        http("upstream", 500),
        http("upstream", 408),
        http("rate_limited", 429),
        http("conflict", 409),
        http("overloaded", 500, code="high_demand"),
        ProviderError("transient_network", retryable=True),
        # A local check after a 200 (Google accepted): no provider status.
        ProviderError("permission", retryable=False, native_code="gcs_token_echo"),
        ProviderError("invalid_request", retryable=False, native_code="explicit_tools_required"),
    ],
)
async def test_maybe_accepted_create_failures_stay_unknown(error: ProviderError) -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.extend([error, {"id": "unexpected", "status": "completed"}])
    with pytest.raises(ProviderError):
        await FlashTransport(source, accounting).create(request())
    assert len(source.requests) == 1
    assert [(r.outcome, r.http_status) for r in accounting.receipts] == [("unknown", None)]


CREATED = "2026-10-23T11:39:56Z"


INPUT: JsonValue = [{"type": "user_input"}]


def cancelled(**extra: JsonValue) -> dict[str, JsonValue]:
    # Cancelled before it ever ran, as Gemini reads it back: unbilled, only the input step.
    return {
        "id": "native",
        "status": "cancelled",
        "created": CREATED,
        "updated": CREATED,
        "steps": INPUT,
        **extra,
    }


class Releasing(Accounting):
    def __init__(self) -> None:
        super().__init__()
        self.released: list[str] = []

    async def release_unbilled(self, *, interaction_id: str) -> bool:
        self.released.append(interaction_id)
        return True


@pytest.mark.parametrize("via", ["cancel", "get"])
async def test_a_cancelled_interaction_without_usage_releases_its_hold(via: str) -> None:
    source, accounting = FakeTransport(), Releasing()
    source.responses.append({"id": "native", "status": "in_progress"})
    policy = FlashTransport(source, accounting)
    await policy.create(request())
    source.reads["native"] = [cancelled()]
    await getattr(policy, via)("native")
    assert accounting.released == ["native"]


@pytest.mark.parametrize(
    "reply",
    [
        cancelled(usage={"total_tokens": 9}),
        # It ran before the cancel landed (the GET -> cancel race): keep the hold.
        cancelled(steps=[{"type": "user_input"}, {"type": "thought"}]),
        cancelled(steps="malformed"),
        {**cancelled(), "status": "in_progress"},
        {**cancelled(), "status": "completed"},
    ],
    ids=["billed", "ran", "malformed-steps", "live", "completed"],
)
async def test_billed_or_live_interactions_keep_their_hold(reply: Mapping[str, JsonValue]) -> None:
    source, accounting = FakeTransport(), Releasing()
    source.responses.append({"id": "native", "status": "in_progress"})
    policy = FlashTransport(source, accounting)
    await policy.create(request())
    source.reads["native"] = [reply]
    await policy.cancel("native")
    assert accounting.released == []


async def test_another_transports_cancel_still_releases_the_hold() -> None:
    # A deadline cleanup or later poll runs on a fresh transport with no model map.
    source, accounting = FakeTransport(), Releasing()
    source.saved["native"] = cancelled()
    await FlashTransport(source, accounting).get("native")
    assert accounting.released == ["native"]


async def test_a_failed_release_never_breaks_the_call() -> None:
    class Failing(Releasing):
        async def release_unbilled(self, *, interaction_id: str) -> bool:
            raise RuntimeError("ledger down")

    source, accounting = FakeTransport(), Failing()
    source.responses.append({"id": "native", "status": "in_progress"})
    policy = FlashTransport(source, accounting)
    await policy.create(request())
    source.reads["native"] = [cancelled()]
    assert (await policy.cancel("native"))["status"] == "cancelled"


@pytest.mark.parametrize(
    ("native_code", "rejected"),
    [("quota_zero", 429), ("quota_exhausted", 429), (None, None)],
)
def test_a_429_naming_its_quota_is_a_definite_rejection(
    native_code: str | None, rejected: int | None
) -> None:
    """Google refused a QuotaFailure before any interaction: its hold frees at once.
    A bare 429 may still have been accepted, so it stays unknown."""
    from mux.drivers.gemini.flash import definite_rejection

    error = ProviderError(
        "rate_limited",
        retryable=native_code != "quota_zero",
        native_code=native_code,
        native_error_code="http_429",
    )
    assert definite_rejection(error) == rejected


class Interrupted(FakeTransport):
    """A create that ends without any answer from Google (a timeout or a cancel)."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def create(self, request: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        self.requests.append(dict(request))
        raise self.error


@pytest.mark.parametrize("error", [TimeoutError(), __import__("asyncio").CancelledError()])
async def test_an_interrupted_create_still_publishes_one_unknown_receipt(
    error: BaseException,
) -> None:
    accounting = Accounting()
    with pytest.raises(type(error)):
        await FlashTransport(Interrupted(error), accounting).create(request())
    # Google may have accepted it: kept as unknown, never silently dropped.
    assert [(r.outcome, r.sent) for r in accounting.receipts] == [("unknown", True)]


async def test_a_malformed_acceptance_still_publishes_one_unknown_receipt() -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.append({"id": "", "status": "in_progress"})
    with pytest.raises(ValueError):
        await FlashTransport(source, accounting).create(request())
    assert [r.outcome for r in accounting.receipts] == ["unknown"]


async def test_a_create_whose_connection_never_opened_is_refused_as_unsent() -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.append(
        ProviderError("transient_network", retryable=True, native_code="connect_failed")
    )
    with pytest.raises(ProviderError):
        await FlashTransport(source, accounting).create(request())
    (receipt,) = accounting.receipts
    assert (receipt.outcome, receipt.sent, receipt.http_status) == ("refused", False, None)


async def test_a_network_failure_after_the_send_stays_unknown() -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.append(ProviderError("transient_network", retryable=True))
    with pytest.raises(ProviderError):
        await FlashTransport(source, accounting).create(request())
    assert [(r.outcome, r.sent) for r in accounting.receipts] == [("unknown", True)]


async def test_the_503_chain_still_publishes_one_receipt_per_attempt() -> None:
    source, accounting = FakeTransport(), Accounting()
    source.responses.extend(
        [
            ProviderError("overloaded", retryable=True, native_code="503"),
            {"id": "interaction", "status": "completed"},
        ]
    )
    await FlashTransport(source, accounting).create(request())
    assert [r.outcome for r in accounting.receipts] == ["refused", "accepted"]


def test_only_a_failed_connection_is_classified_as_never_sent() -> None:
    import httpx
    from mux.drivers.gemini.transport import normalize_error

    url = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/interactions")
    assert (
        normalize_error(httpx.ConnectError("refused", request=url)).native_code == "connect_failed"
    )
    assert (
        normalize_error(httpx.ConnectTimeout("slow", request=url)).native_code == "connect_failed"
    )
    wrapped = httpx.HTTPError("wrapped")
    wrapped.__cause__ = httpx.ConnectError("refused", request=url)
    assert normalize_error(wrapped).native_code == "connect_failed"
    # The request may have been sent: a read timeout or a dropped reply is unknown.
    assert normalize_error(httpx.ReadTimeout("slow", request=url)).native_code is None
    assert normalize_error(httpx.RemoteProtocolError("dropped", request=url)).native_code is None

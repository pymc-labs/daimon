"""Actual SDK request bytes and driver state for the bounded round scenarios."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
from mux.contracts.actions import InputEvent, UserMessage
from mux.contracts.events import Event, TextPart
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import SendReceipt
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.core import GeminiEvents
from mux.drivers.gemini.transport import NO_TOOLS_TOOL, Object, SDKTransport
from mux.testing.conformance.budget import BudgetConfig, BudgetGuard, BudgetRefused
from mux.testing.conformance.runner import ConformanceFailure
from mux.testing.drivers.gemini.round_cases import INTERACTION_CEILING_USD, run_c04, run_c06
from mux.testing.drivers.gemini.round_runner import Admission, Case, Envelope, execute
from mux.testing.isolated_sdk import build_gemini_client
from pydantic import JsonValue

SCOPE = Scope(tenant_id="n12-round", account_id="qa", principal_id="human", authorization_id="go")


class NativeWire:
    def __init__(self, *, terminal: str = "cancelled", initial: str = "in_progress") -> None:
        self.terminal, self.initial = terminal, initial
        self.requests: list[httpx.Request] = []
        self.posts: list[bytes] = []
        self.cancels: list[bytes] = []
        # Gemini workspaces expire seven days after activity: stamp from now.
        self.created = datetime.now(UTC).replace(microsecond=0)
        self.stamp = self.created
        self.session: ResourceRef | None = None
        self.sdk: SDKTransport | None = None
        self.events: tuple[Event, ...] = ()
        self.cancelled = False
        self.identities: list[str] = []
        self.deletes: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            native_id = request.url.path.rsplit("/", 1)[-1]
            assert native_id in self.identities
            self.deletes.append(native_id)
            return httpx.Response(200, json={})
        if request.method == "POST" and request.url.path.endswith("/interactions"):
            self.posts.append(request.content)
            if self.sdk is not None:
                assert self.session is not None
            body = json.loads(request.content)
            assert body["tools"] == [NO_TOOLS_TOOL]
            assert body["agent_config"]["model"] == "gemini-3.8-flash"
            self.identities.append(
                "native-owned" if len(self.posts) == 1 else f"native-owned-{len(self.posts)}"
            )
        elif request.method == "POST":
            assert request.url.path.endswith(("/native-owned:cancel", "/native-owned/cancel"))
            self.cancels.append(request.content)
            self.cancelled = True
        else:
            assert request.method == "GET"
            assert request.url.path.rsplit("/", 1)[-1] in self.identities
            if request.url.params.get("stream") == "true":
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, content=b""
                )
        self.stamp += timedelta(seconds=1)
        return httpx.Response(
            200,
            json={
                "id": self.identities[-1]
                if request.method == "POST"
                else request.url.path.rsplit("/", 1)[-1],
                "status": self.terminal if self.cancelled else self.initial,
                "environment_id": "native-workspace",
                "created": self.created.isoformat(),
                "updated": self.stamp.isoformat(),
                "steps": [],
                "usage": {
                    "total_input_tokens": 64,
                    "total_cached_tokens": 0,
                    "total_output_tokens": 8,
                    "total_thought_tokens": 3,
                },
            },
        )

    def bind_session(self, session: ResourceRef) -> None:
        assert session.tenant_id == SCOPE.tenant_id and session.account_id == SCOPE.account_id
        self.session = session

    def attach_driver(self, driver: GeminiManagedAgents, session: ResourceRef) -> None:
        assert driver.capabilities().provider == "gemini" and self.session == session

    @property
    def native_posts(self) -> int:
        return len(self.posts)

    @property
    def cancel_posts(self) -> int:
        return len(self.cancels)

    def record(self, events: tuple[Event, ...]) -> None:
        self.events += events

    def verify_receipt(
        self, session: ResourceRef, receipt: SendReceipt, inputs: Sequence[InputEvent]
    ) -> bool:
        if len(receipt.input_ids) != 1 or receipt.turn_id != receipt.input_ids[0]:
            return False
        index = self.identities.index(receipt.input_ids[0])
        expected = [
            {
                "type": "text",
                "text": "".join(p.text for p in item.content if isinstance(p, TextPart)),
            }
            for item in inputs
            if isinstance(item, UserMessage)
        ]
        return json.loads(self.posts[index])["input"] == expected

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        return await cast(SDKTransport, self.sdk).create(request)

    async def get(self, interaction_id: str) -> Object:
        return await cast(SDKTransport, self.sdk).get(interaction_id)

    async def cancel(self, interaction_id: str) -> Object:
        return await cast(SDKTransport, self.sdk).cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        raise AssertionError("C06 cannot download a workspace")

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        return await cast(SDKTransport, self.sdk).open_stream(interaction_id, after=after)


@pytest.mark.asyncio
async def test_c06_real_sdk_request_cancel_barrier_and_native_identity() -> None:
    native = NativeWire()
    with build_gemini_client(
        transport=httpx.MockTransport(native.handle), api_key="offline-round"
    ) as sdk:
        native.sdk = SDKTransport(sdk)
        try:
            result = await run_c06(native, SCOPE)
        finally:
            await sdk.aio.aclose()
    assert result.status == "pass" and result.fixture_id == "C06"
    assert len(native.posts) == len(native.cancels) == 1
    assert sum(r.url.params.get("stream") == "true" for r in native.requests) == 1
    ends = [e for e in native.events if e.type == "session.turn_ended"]
    assert len(ends) == 1 and ends[0].turn_id == "native-owned"
    assert ends[0].payload["outcome"] == "interrupted"
    assert json.loads(native.posts[0])["input"][0]["type"] == "text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "terminal"), [("completed", "cancelled"), ("in_progress", "completed")]
)
async def test_c06_never_awards_completed_race_as_cancelled(initial: str, terminal: str) -> None:
    native = NativeWire(initial=initial, terminal=terminal)
    with build_gemini_client(
        transport=httpx.MockTransport(native.handle), api_key="offline-round"
    ) as sdk:
        native.sdk = SDKTransport(sdk)
        try:
            with pytest.raises(ConformanceFailure):
                await run_c06(native, SCOPE)
        finally:
            await sdk.aio.aclose()
    assert len(native.posts) == 1


def test_approved_model_limit_bound_includes_thinking_and_no_cache_discount() -> None:
    assert Decimal("1.032192") == INTERACTION_CEILING_USD


@pytest.mark.asyncio
async def test_c04_actual_sdk_acceptance_and_local_four_boundary_envelope() -> None:
    native = NativeWire(initial="completed")
    with build_gemini_client(
        transport=httpx.MockTransport(native.handle), api_key="offline-round"
    ) as sdk:
        native.sdk = SDKTransport(sdk)
        try:
            result = await run_c04(native, SCOPE)
        finally:
            await sdk.aio.aclose()
    assert result.status == "pass" and result.fixture_id == "C04"
    assert len(native.posts) == len(set(native.identities)) == 3
    assert not native.cancels
    assert len({json.loads(body)["input"][0]["text"] for body in native.posts}) == 3
    assert {e.turn_id for e in native.events if e.type == "user.message"} == {
        "native-owned",
        "native-owned-3",
    }  # Lost acknowledgement cannot forge a user/root journal row.


def admission(tmp_path: Path, case: Case) -> Admission:
    today = datetime.now(UTC)
    ledger, config = tmp_path / "spend.md", tmp_path / "budget.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "total_cap_usd": "5",
                "ledger_path": str(ledger),
                "providers": {
                    "gemini": {
                        "cap_usd": "5",
                        "models": {
                            "gemini-3.8-flash": {
                                "input": "0.75",
                                "output": "3.75",
                                "cached_input": "0.075",
                                "cache_write_input": "0.75",
                                "source": "https://ai.google.dev/gemini-api/docs/pricing",
                                "effective_from": today.date().isoformat(),
                                "effective_until": (today + timedelta(days=1)).date().isoformat(),
                                "pricing_basis": "standard-global",
                            }
                        },
                    }
                },
            }
        )
    )
    BudgetGuard.initialize(config, ledger)
    guard = BudgetGuard(config, ledger)
    anchor = json.loads(guard.lock_path.read_text())
    n = 1 if case == "C06" else 3
    envelope = Envelope(
        case=case,
        model="gemini-3.8-flash",
        source_head="1" * 40,
        config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        ledger_id=anchor["binding"]["ledger_id"],
        scope=SCOPE,
        interactions=n,
        input_tokens_per_interaction=1048576,
        output_tokens_per_interaction=65536,
        maximum_usd=INTERACTION_CEILING_USD * n,
        deadline_seconds=30,
        expires_at=today + timedelta(minutes=5),
    )
    assert BudgetConfig.load(config).total_cap_usd == 5
    return Admission(envelope, config, tmp_path / case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["C06", "C04"])
async def test_complete_round_sdk_single_case_hold_get_before_delete_and_exact_spend(
    tmp_path: Path, case: Case
) -> None:
    native = NativeWire(initial="in_progress" if case == "C06" else "completed")
    selected = admission(tmp_path, case)
    result = await execute(selected, mock=httpx.MockTransport(native.handle))
    assert result["status"] == "pass" and result["origin"] == "sdk-mock"
    assert len(native.posts) == len(native.deletes) == selected.envelope.interactions
    assert result["native_creates"] == selected.envelope.interactions
    assert result["actual_usd"] == str(Decimal("0.00008925") * selected.envelope.interactions)
    assert result["held_usd"] == "0"
    receipts = BudgetGuard(selected.config, tmp_path / "spend.md").report()
    assert len(receipts) == 1 and receipts[0].fixture_id == case
    for index, request in enumerate(native.requests):
        if request.method == "DELETE":
            prior = native.requests[index - 1]
            assert prior.method == "GET" and prior.url.path == request.url.path
    facts = result["interactions"]
    assert isinstance(facts, list) and len(facts) == selected.envelope.interactions
    assert all(
        isinstance(row, dict)
        and row["final_get"]
        and row["complete"]
        and row["private_audit_retained"]
        for row in facts
    )
    private = tmp_path / ".gemini-round-private" / case
    assert len(tuple(private.iterdir())) == selected.envelope.interactions
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in private.iterdir())
    for request in native.requests:
        if request.method == "POST" and request.url.path.endswith("/interactions"):
            body = json.loads(request.content)
            assert (
                body["tools"] == [NO_TOOLS_TOOL]
                and body["agent_config"]["max_total_tokens"] == 4096
            )
    with pytest.raises(FileExistsError):
        await execute(selected, mock=httpx.MockTransport(native.handle))
    assert len(native.posts) == selected.envelope.interactions


@pytest.mark.asyncio
async def test_unsealed_live_admission_refuses_before_key_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = admission(tmp_path, "C06")

    def forbidden() -> str:
        raise AssertionError("key must not be touched")

    monkeypatch.setattr("mux.testing.drivers.gemini.round_runner.read_key", forbidden)
    with pytest.raises(BudgetRefused, match="separately pinned"):
        await execute(selected, live=True)
    assert not selected.output.exists()
    assert not BudgetGuard(selected.config, tmp_path / "spend.md").report()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["null-root", "foreign-root", "foreign-session", "premature-stop"]
)
async def test_c06_native_scope_and_eof_mutants_are_rejected(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_cancel, original_wait = GeminiEvents.cancel, GeminiEvents.wait_stopped

    async def wrong_cancel(self, scope, session, *, turn_id, key):
        receipt = await original_cancel(self, scope, session, turn_id=turn_id, key=key)
        if mutation == "foreign-session":
            return receipt.model_copy(
                update={"session": session.model_copy(update={"tenant_id": "foreign"})}
            )
        return receipt.model_copy(
            update={"turn_id": None if mutation == "null-root" else "foreign-root"}
        )

    async def premature_stop(self, scope, receipt, *, deadline):
        actual = await original_wait(self, scope, receipt, deadline=deadline)
        return actual.model_copy(update={"stopped": True, "outcome": "interrupted"})

    if mutation == "premature-stop":
        monkeypatch.setattr(GeminiEvents, "wait_stopped", premature_stop)
    else:
        monkeypatch.setattr(GeminiEvents, "cancel", wrong_cancel)
    native = NativeWire()
    with build_gemini_client(
        transport=httpx.MockTransport(native.handle), api_key="offline-round"
    ) as sdk:
        native.sdk = SDKTransport(sdk)
        try:
            with pytest.raises(ConformanceFailure):
                await run_c06(native, SCOPE)
        finally:
            await sdk.aio.aclose()
    assert len(native.posts) == len(native.cancels) == 1

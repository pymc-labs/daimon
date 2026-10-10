from __future__ import annotations

import json
import subprocess
from decimal import Decimal

import pytest
from pydantic import JsonValue, ValidationError

from qa.live.billing import BillingEvidence, approved_rates, footer_matches, totals
from qa.live.config import Config, Pricing
from qa.live.discord import DiscordBackend
from qa.live.evaluate import evaluate
from qa.live.models import ModelPolicy
from qa.live.schema import Assertion
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Message, Pending, Turn, utcnow


def example() -> BillingEvidence:
    return BillingEvidence(
        {
            "id": "outcome",
            "model_calls": 1,
            "usage_refs": [{"session_id": "session", "event_id": "event"}],
        },
        [
            {
                "managed_session_id": "session",
                "event_id": "event",
                "model": "claude-haiku-5-5",
                "input_tokens": 4,
                "cache_creation_input_tokens": 21301,
                "cache_read_input_tokens": 0,
                "output_tokens": 8,
                "ledger_id": "ledger",
                "idempotency_key": "turn:session:event",
                "ledger_reason": "turn_debit",
                "delta_usd": "-0.002934",
            }
        ],
        Decimal("1.1"),
        approved_rates(),
        ModelPolicy(),
    )


def test_independent_request_prices_short_long_and_snapshot() -> None:
    e = example()
    assert totals(e) == (Decimal("0.0029337275"), Decimal("0.002934"))
    e.requests[0].update(
        model="claude-haiku-5-5-20261001",
        cache_creation_input_tokens=181716,
        cache_read_input_tokens=20221,
        output_tokens=9,
        delta_usd="-0.126069",
    )
    assert totals(e) == (Decimal("0.126068855"), Decimal("0.126069"))
    # Choose the tier per request, not from aggregate prompt size.
    e.requests[0].update(
        input_tokens=50000,
        cache_creation_input_tokens=50000,
        cache_read_input_tokens=0,
        output_tokens=0,
    )
    assert totals(e)[0] == Decimal("0.012375")
    e.requests[0]["cache_read_input_tokens"] = 1
    assert totals(e)[0] == Decimal("0.061875055")


@pytest.mark.parametrize(
    "fault",
    ["missing", "duplicate", "refs", "unknown", "tokens", "key", "credit", "markup", "calls"],
)
def test_missing_or_ambiguous_billing_evidence_never_passes(fault: str) -> None:
    e = example()
    if fault == "missing":
        e.requests = []
    if fault == "duplicate":
        e.requests *= 2
    if fault == "refs":
        e.outcome["usage_refs"] = []
    if fault == "unknown":
        e.requests[0]["model"] = "unpriced-model"
    if fault == "tokens":
        e.requests[0]["input_tokens"] = None
    if fault == "key":
        e.requests[0]["idempotency_key"] = "foreign"
    if fault == "credit":
        e.requests[0]["delta_usd"] = "1"
    if fault == "markup":
        e.markup = Decimal("NaN")
    if fault == "calls":
        e.outcome["model_calls"] = 2
    with pytest.raises(Pending):
        totals(e)


def test_declared_tolerances_and_footer_are_independent() -> None:
    e = example()

    class Backend(FakeBackend):
        def billing_evidence(self, turn: Turn) -> BillingEvidence:
            return e

    turn = Turn(1, "trigger", "parent", utcnow(), settled=True)
    turn.messages = [
        {"content": "$99 used", "embeds": [{"footer": {"text": "$0.003 used $130 left"}}]}
    ]
    footer = Assertion(kind="footer_cost_matches_ledger", turn=1, tol_usd=0.001)
    usage = Assertion(kind="ledger_matches_usage", turn=1, tol_pct=2)
    assert evaluate(footer, [turn], Backend(), FakeJudge()).status == "PASS"
    assert evaluate(usage, [turn], Backend(), FakeJudge()).status == "PASS"
    e.requests[0]["delta_usd"] = "-0.0031"
    assert evaluate(usage, [turn], Backend(), FakeJudge()).status == "FAIL"
    # Rounding within the explicit absolute tolerance remains accepted.
    assert footer_matches(turn, Decimal("0.002934"), 0.001)
    assert not footer_matches(turn, Decimal("0.009"), 0.001)
    turn.messages = [{"embeds": [{"footer": {"text": "<$0.001 used $9 left"}}]}]
    assert footer_matches(turn, Decimal("0.000999"), 0)
    assert not footer_matches(turn, Decimal("0.001"), 0.001)
    turn.messages = [{"content": "$0.003 used", "embeds": [{"footer": {"text": "$130 left"}}]}]
    with pytest.raises(Pending):
        footer_matches(turn, Decimal("0.003"), 0.001)


@pytest.mark.parametrize("kind", ["footer_cost_matches_ledger", "ledger_matches_usage"])
def test_billing_tolerance_is_required(kind: str) -> None:
    with pytest.raises(ValidationError):
        Assertion.model_validate({"kind": kind, "turn": 1})


@pytest.mark.parametrize("fault", [None, "outcomes", "foreign", "ledger", "probe"])
def test_backend_reads_exact_refs_and_scoped_debits(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    config = Config(
        pricing=Pricing(
            per_turn_usd=0.02, judge_input_per_million=0.1, judge_output_per_million=0.5
        )
    )
    config.staging.enabled = True
    config.staging.billing_probe = ["read-only-markup"]
    backend = DiscordBackend(config, "staging")
    backend.owned.add("parent")
    backend.threads.add("thread")
    backend.thread_parents["thread"] = "parent"
    turn = Turn(
        1,
        "trigger",
        "parent",
        utcnow(),
        thread_id="thread",
        ended_at=utcnow(),
        guild_id=config.staging.guild_id,
        settled=True,
    )
    tenant = "449f4dc5-5990-52c0-96e0-45a4ab3bf0e4"
    e = example()
    e.outcome.update(
        tenant_id=tenant,
        thread_id="thread",
        started_at=turn.started_at.isoformat(),
        ended_at=turn.ended_at.isoformat(),
    )
    e.requests[0].update(
        tenant_id=tenant, ledger_tenant=tenant, channel_id="parent", ledger_channel="parent"
    )
    queries: list[str] = []

    async def query(sql: str, params: Message) -> JsonValue:
        queries.append(sql)
        assert params["tenant"] == tenant
        if "SELECT id,tenant_id" in sql:
            return [] if fault == "outcomes" else [e.outcome]
        if fault == "foreign":
            e.requests[0]["ledger_channel"] = "foreign"
        if fault == "ledger":
            e.requests[0]["ledger_id"] = None
        return e.requests

    def command(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            1 if fault == "probe" else 0,
            json.dumps(
                {
                    "markup": "1.1",
                    "source": "deployed billing.markup",
                    "guild_id": config.staging.guild_id,
                }
            ),
            "",
        )

    monkeypatch.setattr(backend, "_query", query)
    monkeypatch.setattr(subprocess, "run", command)
    if fault:
        with pytest.raises(Pending):
            backend.billing_evidence(turn)
    else:
        assert totals(backend.billing_evidence(turn))[1] == Decimal("0.002934")
        assert len(queries) == 2 and "jsonb_array_elements(o.usage_refs)" in queries[1]
        assert "l.idempotency_key='turn:'" in queries[1]
        backend.billing_evidence(turn)
        assert len(queries) == 2 and turn.billing["markup"] == "1.1"

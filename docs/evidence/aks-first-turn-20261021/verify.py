"""Compare provider edge, C3 receipts and Postgres, without making model calls."""

import json
import re
from decimal import Decimal
from pathlib import Path

root = Path(__file__).resolve().parent
provider = []
for line in (root / "harness-turn-window.log").read_text().splitlines():
    if '"event": "harness.azure_usage"' in line:
        provider.append(json.loads(line[line.index("{") :]))
events = json.loads((root / "c3-events.json").read_text())
usage = [event for event in events if event["type"] == "usage"]
starts = [event for event in events if event["type"] == "model_request_started"]
db = json.loads((root / "postgres-evidence.json").read_text())
assert len(provider) == len(usage) == len(starts) == len(db["usage_events"]) == 4
assert len({event["session_id"] for event in events}) == 1
assert sum(row["applied_at"] is not None for row in db["accounting_outbox"]) == 4
assert all(row["applied_at"] is not None for row in db["accounting_outbox"] if row["revision"] == 2)
for before, event in zip(starts, usage, strict=True):
    assert before["data"]["request_id"] == event["data"]["request_id"]
    assert before["seq"] < event["seq"]
    native = event["data"]
    # Counts uniquely identify these four provider receipts; no positional join.
    matches = [
        receipt
        for receipt in provider
        if (
            receipt["usage"]["input_tokens"],
            receipt["usage"]["output_tokens"],
            receipt["usage"]["input_tokens_details"]["cached_tokens"],
        )
        == (native["input_tokens"], native["output_tokens"], native["cached_input_tokens"])
    ]
    assert len(matches) == 1
    rows = [row for row in db["usage_events"] if row["event_id"].endswith(native["request_id"])]
    assert len(rows) == 1
    row = rows[0]
    assert row["input_tokens"] + row["cache_read_input_tokens"] == native["input_tokens"]
    assert row["output_tokens"] == native["output_tokens"]
    assert row["cache_read_input_tokens"] == native["cached_input_tokens"]
    debits = [
        entry for entry in db["tenant_ledger"] if native["request_id"] in entry["idempotency_key"]
    ]
    assert len(debits) == 1 and debits[0]["reason"] == "turn_debit"
    print(
        native["request_id"],
        native["input_tokens"],
        native["cached_input_tokens"],
        native["output_tokens"],
        debits[0]["delta_usd"],
        matches[0]["response_id"],
    )
for filename, command in [("turn1.ndjson", None), ("turn2.ndjson", "cat aks-l15-proof.txt")]:
    terminal = json.loads((root / filename).read_text().splitlines()[-1])
    assert terminal["status"] == "end_turn"
    blocks = terminal["state"]["content"]
    assert blocks[-1]["text"] == "AKS_L15_PERSIST_20261021"
    assert blocks[0]["name"] == "bash" and blocks[0]["is_error"] is False
    if command:
        assert blocks[0]["input"]["command"] == command
logs = (root / "coredns-turn-window.log").read_text()
assert not re.search(r"(?:^|[.\s])(?:anthropic|openai)\.com\.", logs, re.I)
assert "daimon-selfhost-aoai.cognitiveservices.azure.com." in logs
probes = json.loads((root / "curl-results.json").read_text())
assert probes[0]["returncode"] == 28 and probes[1]["returncode"] == 0
amount = sum(
    Decimal(str(row["delta_usd"])) for row in db["tenant_ledger"] if row["reason"] == "turn_debit"
)
print("PASS: tool, persistence, usage and debit checks; no forbidden DNS during window.")
print("TEST tariff debits USD:", amount)
print("GAP: host curl reached Anthropic; harness curl was blocked.")

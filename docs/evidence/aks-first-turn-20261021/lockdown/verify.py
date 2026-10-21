"""Verify the single paid turn after both pod classes were locked down."""

import json
import re
from pathlib import Path

root = Path(__file__).resolve().parent
probes = json.loads((root / "curl-results.json").read_text())
assert len(probes) == 2 and all(probe["returncode"] == 28 for probe in probes)
assert all(probe["stdout"] == "HTTP=000\n" for probe in probes)
events = json.loads((root / "c3-events.json").read_text())
assert sum(event["type"] == "model_request_started" for event in events) == 1
assert sum(event["type"] == "usage" for event in events) == 1
assert events[-1]["type"] == "turn_ended"
usage = next(event["data"] for event in events if event["type"] == "usage")
provider = [
    json.loads(line[line.index("{") :])
    for line in (root / "harness.log").read_text().splitlines()
    if '"event": "harness.azure_usage"' in line
]
assert len(provider) == 1
assert provider[0]["model"] == usage["model"] == "gpt-5.4-mini"
assert provider[0]["usage"]["input_tokens"] == usage["input_tokens"] == 2244
assert provider[0]["usage"]["output_tokens"] == usage["output_tokens"] == 7
assert provider[0]["usage"]["input_tokens_details"]["cached_tokens"] == 0
rows = json.loads((root / "postgres.json").read_text())
metered = [row for row in rows["usage_events"] if row["event_id"].endswith(usage["request_id"])]
assert len(metered) == 1
assert metered[0]["input_tokens"] == 2244 and metered[0]["output_tokens"] == 7
debits = [row for row in rows["tenant_ledger"] if usage["request_id"] in row["idempotency_key"]]
assert len(debits) == 1 and debits[0]["reason"] == "turn_debit"
terminal = json.loads((root / "lockdown-turn.ndjson").read_text().splitlines()[-1])
assert terminal["status"] == "end_turn" and terminal["state"]["error"] is None
assert terminal["state"]["content"] == [{"kind": "text", "text": "LOCKDOWN_OK"}]
assert terminal["session_id"] == "sh_session_57d9b493-8a0e-53cc-a4d4-f03cfc23f994"
logs = (root / "coredns.log").read_text()
assert not re.search(r"(?:^|[.\s])(?:anthropic|openai)\.com\.", logs, re.I)
print(
    "PASS: both curls blocked, one real turn, matching AOAI/C3/Postgres usage, zero forbidden DNS."
)
print("request_id:", usage["request_id"], "TEST tariff debit USD:", debits[0]["delta_usd"])

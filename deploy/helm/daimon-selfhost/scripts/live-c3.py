"""Operator-run gpt-5.4-mini C3 preflight; executes inside the MCP container."""

import json
import os
import urllib.request
from uuid import uuid4

if os.environ.get("AOAI_VERIFIED_MODEL") != "gpt-5.4-mini":
    raise SystemExit("Probe requires AOAI_VERIFIED_MODEL=gpt-5.4-mini from deployment metadata")

deployment = os.environ.get("AOAI_DEPLOYMENT", "gpt-5.4-mini")
model = os.environ.get("SELFHOSTED_MODEL", deployment)
if "sol" in model.lower() or "sol" in deployment.lower() or model != deployment:
    raise SystemExit("Probe requires the configured AOAI_DEPLOYMENT; Sol models are refused")

base = "https://coke-daimon-harness:8088"
token = os.environ["DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY"]
session = "aks-preflight-" + uuid4().hex
marker = "daimon-aks-proof-" + uuid4().hex
proof_path = "daimon-aks-proof"
for tool_name, instruction in (
    (
        "write",
        f"Use the write tool to create {proof_path} containing exactly {marker}, then confirm.",
    ),
    (
        "read",
        f"Use the read tool to read {proof_path} from the previous turn; quote its exact contents.",
    ),
):
    turn = uuid4().hex
    body = dict(
        session_id=session,
        turn_id=turn,
        idempotency_key=turn,
        model=model,
        instructions="You are testing persistent tool execution. Follow the request.",
        input=[dict(type="text", text=instruction)],
        mcp_servers=[],
        skills=[],
        limits=dict(max_tool_calls=8, deadline_s=120),
    )
    req = urllib.request.Request(
        base + "/v0/turns",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=150) as response:
        events = [
            json.loads(line[6:])
            for line in response.read().decode().splitlines()
            if line.startswith("data: ")
        ]
    starts = [e["data"]["request_id"] for e in events if e["type"] == "model_request_started"]
    usage = [e["data"] for e in events if e["type"] == "usage"]
    assert sorted(starts) == sorted(u["request_id"] for u in usage), "Usage inventory mismatch"
    assert len(starts) == len(set(starts)) and starts, "No model requests or duplicate inventory"
    assert events[-1]["type"] == "turn_ended" and events[-1]["data"]["outcome"] == "completed"
    calls = {
        e["data"]["call_id"]
        for e in events
        if e["type"] == "tool_use"
        and e["data"]["name"] == tool_name
        and e["data"]["input"].get("path") == proof_path
        and (tool_name != "write" or e["data"]["input"].get("content") == marker)
    }
    assert any(
        e["type"] == "tool_result"
        and e["data"]["call_id"] in calls
        and not e["data"]["is_error"]
        and (tool_name != "read" or e["data"]["output"] == marker)
        for e in events
    ), f"Missing successful {tool_name} of the exact persistence marker"
    for e in events:
        if e["type"] in {"agent_message", "usage", "turn_ended"}:
            print(json.dumps(e))
print(f"{model} C3 preflight completed; this is not a host ledger/CLI turn proof")

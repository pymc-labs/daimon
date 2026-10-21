"""Exercise verified HTTPS C3, MCP-header forwarding and checkpoint resume."""

import argparse
import json
import os
import ssl
import time
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="https://127.0.0.1:18088")
parser.add_argument("--ca", required=True)
parser.add_argument("--server-name", default="smoke-daimon-harness.daimon-pods.svc")
parser.add_argument("--output", required=True)
parser.add_argument("--resume", action="store_true")
args = parser.parse_args()
mode = "read" if args.resume else "write"
context = ssl.create_default_context(cafile=args.ca)
extensions = {"sni_hostname": args.server_name.encode()}
with httpx.Client(
    base_url=args.url,
    verify=context,
    trust_env=False,
    timeout=180,
    headers={
        "Authorization": "Bearer " + os.environ["HARNESS_PROBE_TOKEN"],
        "Accept": "application/json",
    },
) as client:
    started = time.monotonic()
    response = client.post(
        "/v0/turns",
        json={
            "session_id": "secure-smoke",
            "turn_id": mode,
            "idempotency_key": mode,
            "model": "fake",
            "instructions": "smoke",
            "input": [{"type": "text", "text": mode}],
            "mcp_servers": [],
            "skills": [],
            "limits": {"max_tool_calls": 3, "deadline_s": 30},
        },
        headers={"X-Daimon-MCP-Authorization": "Bearer synthetic-forwarding-check"},
        extensions=extensions,
    )
    response.raise_for_status()
    for _ in range(300):
        response = client.get("/v0/sessions/secure-smoke/events", extensions=extensions)
        response.raise_for_status()
        events = response.json()["events"]
        terminal = next(
            (e for e in events if e["turn_id"] == mode and e["type"] == "turn_ended"), None
        )
        if terminal:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("turn did not end")
    assert terminal["data"]["outcome"] == "completed", terminal
    assert any(
        "POD_RESUME_OK" in json.dumps(e["data"])
        for e in events
        if e["type"] == "agent_message" and e["turn_id"] == mode
    )
    busy = client.get("/v0/sessions/exceeds-capacity/events", extensions=extensions)
    assert busy.status_code == 503 and busy.headers["retry-after"] == "15"
    Path(args.output).write_text(
        json.dumps(
            {"mode": mode, "seconds": time.monotonic() - started, "events": events}, indent=2
        )
    )
    print(json.dumps({"mode": mode, "ok": True, "events": len(events)}))

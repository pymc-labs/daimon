"""Run inside the MCP pod: real C3 HTTP, fake inference, no Azure."""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

base = "http://smoke-daimon-harness:8088"
headers = {"Authorization": "Bearer offline-kind-only", "Content-Type": "application/json"}


def call(path, body=None, auth=True):
    req = urllib.request.Request(
        base + path,
        headers=headers if auth else {},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode()


try:
    call("/v0/healthz", auth=False)
    raise AssertionError("unauthenticated health was accepted")
except urllib.error.HTTPError as exc:
    assert exc.code == 401, exc.code
assert json.loads(call("/v0/healthz"))["protocol"] == "daimon-harness/0"
evidence = Path(sys.argv[2])
before = sys.argv[1] == "before"
saved = {} if before else json.loads(evidence.read_text())
for session in ("kind-one", "kind-two"):
    body = dict(
        session_id=session,
        turn_id=session,
        idempotency_key=session,
        model="gpt-6-luna",
        instructions="Offline smoke",
        input=[dict(type="text", text="write")],
        mcp_servers=[],
        skills=[],
        limits=dict(max_tool_calls=3, deadline_s=30),
    )
    if not before:
        # GET first: a POST must never reconstruct missing pre-restart state.
        replay = json.loads(call(f"/v0/sessions/{session}/events?after_seq=0"))
        assert replay["complete"] and replay["events"] == saved[session]["events"]
    stream = call("/v0/turns", body)
    if not before:
        assert stream == saved[session]["stream"]
    events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
    assert any(e["type"] == "usage" for e in events), stream
    assert any(e["type"] == "tool_result" and not e["data"]["is_error"] for e in events), stream
    assert events[-1]["type"] == "turn_ended" and events[-1]["data"]["outcome"] == "completed", (
        stream
    )
    assert call("/v0/turns", body) == stream, "idempotent replay differs"
    replay = json.loads(call(f"/v0/sessions/{session}/events?after_seq=0"))
    assert replay["complete"] and replay["events"] == events
    if before:
        saved[session] = dict(stream=stream, events=events)
    else:
        body.update(
            turn_id=session + "-read",
            idempotency_key=session + "-read",
            input=[dict(type="text", text="read")],
        )
        read_stream = call("/v0/turns", body)
        assert '"outcome":"completed"' in read_stream.replace(" ", ""), read_stream
    print(session, "C3 completed: tool write, measured fake usage, replay, idempotency")
if before:
    evidence.write_text(json.dumps(saved))
print("Bearer health and 401 rejection passed")

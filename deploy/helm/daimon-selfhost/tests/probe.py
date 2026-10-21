"""Run inside the MCP pod: real C3 HTTPS, fake inference, no Azure."""

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

base = "https://smoke-daimon-harness:8088"
headers = {"Authorization": "Bearer offline-kind-only", "Content-Type": "application/json"}


def call(path, body=None, auth=True):
    req = urllib.request.Request(
        base + path,
        headers=headers if auth else {},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(
        req, timeout=60, context=ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    ) as response:
        return response.read().decode()


try:
    call("/v0/healthz", auth=False)
    raise AssertionError("unauthenticated health was accepted")
except urllib.error.HTTPError as exc:
    assert exc.code == 401, exc.code
assert json.loads(call("/v0/healthz"))["protocol"] == "daimon-harness/0"
# A valid HTTPS endpoint must be refused under an unrelated CA.
try:
    urllib.request.urlopen(
        base + "/readiness",
        context=ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt"),
        timeout=10,
    )
except urllib.error.URLError as error:
    assert isinstance(error.reason, ssl.SSLCertVerificationError), type(error.reason)
else:
    raise AssertionError("Harness accepted with wrong CA")
print("Harness wrong-CA refusal passed")
evidence = Path(sys.argv[2])
before = sys.argv[1] == "before"
negative = sys.argv[1] == "missing"
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
    if not before and not negative:
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
    assert replay["complete"] and (negative or replay["events"] == events)
    if before:
        saved[session] = dict(stream=stream, events=events)
    else:
        body.update(
            turn_id=session + ("-missing" if negative else "-read"),
            idempotency_key=session + ("-missing" if negative else "-read"),
            input=[dict(type="text", text="read")],
        )
        read_stream = call("/v0/turns", body)
        read_events = [
            json.loads(line[6:]) for line in read_stream.splitlines() if line.startswith("data: ")
        ]
        assert (
            read_events[-1]["type"] == "turn_ended"
            and read_events[-1]["data"]["outcome"] == "completed"
        ), read_stream
        assert any(
            e["type"] == "tool_result"
            and not e["data"]["is_error"]
            and e["data"]["output"] == "persisted"
            for e in read_events
        ), "Missing exact persisted tool result"
    print(session, "C3 completed: tool write, measured fake usage, replay, idempotency")
if before:
    evidence.write_text(json.dumps(saved))
print("Bearer health and 401 rejection passed")

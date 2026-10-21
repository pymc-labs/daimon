"""Run against a port-forwarded controller; save timing and recovery receipts."""

import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:18088")
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--deployment", default="smoke-daimon-harness")
parser.add_argument("--resume", action="store_true")
args = parser.parse_args()
run_id = (
    json.loads(args.output.read_text())["session_prefix"]
    if args.resume
    else "run-" + uuid.uuid4().hex[:8] + "-"
)
name = args.deployment
ns = "daimon-pods"
client = httpx.Client(
    base_url=args.url,
    headers={"Authorization": "Bearer " + os.environ.get("L16_SMOKE_TOKEN", "offline-l16")},
    timeout=200,
    limits=httpx.Limits(max_keepalive_connections=0),
)


def kube(*words):
    return subprocess.check_output(["kubectl", "-n", ns, *words], text=True).strip()


def pods():
    return json.loads(kube("get", "pods", "-l", "daimon-controller=" + name, "-o", "json"))["items"]


def wait_warm():
    stop = time.time() + 120
    while time.time() < stop:
        if any(
            p["metadata"]["labels"]["daimon-session"] == "warm"
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p.get("status", {}).get("conditions", [])
            )
            for p in pods()
        ):
            return
        time.sleep(0.5)
    raise AssertionError("warm pool not ready")


def bind(session):
    session = run_id + session
    start = time.monotonic()
    response = client.get(
        f"/v0/sessions/{session}/events", params={"session_id": session, "after_seq": 0}
    )
    assert response.status_code == 200, response.text
    return time.monotonic() - start


def sandbox(session):
    session = run_id + session
    from mux.drivers.selfhosted.transport import sandbox_id

    return next(
        p["metadata"]["name"]
        for p in pods()
        if p["metadata"]["labels"]["daimon-session"] == sandbox_id(session)
    )


def turn(session, mode):
    session = run_id + session
    turn_id = "t-" + uuid.uuid4().hex
    response = client.post(
        "/v0/turns",
        params={"session_id": session, "after_seq": 0},
        headers={"Accept": "application/json"},
        json={
            "session_id": session,
            "turn_id": turn_id,
            "idempotency_key": turn_id,
            "model": "gpt-6-luna",
            "instructions": "",
            "input": [{"type": "text", "text": mode}],
            "mcp_servers": [],
            "skills": [],
            "limits": {"max_tool_calls": 2, "deadline_s": 30},
        },
    )
    assert response.status_code == 202, response.text
    if mode == "oom":
        return
    stop = time.time() + 60
    while time.time() < stop:
        response = client.get(
            f"/v0/sessions/{session}/events", params={"session_id": session, "after_seq": 0}
        )
        assert response.status_code == 200, response.text
        events = response.json()["events"]
        if any(e["turn_id"] == turn_id and e["type"] == "turn_ended" for e in events):
            terminal = next(
                e for e in events if e["turn_id"] == turn_id and e["type"] == "turn_ended"
            )
            assert terminal["data"]["outcome"] == "completed", terminal
            return [e for e in events if e["turn_id"] == turn_id]
        time.sleep(0.1)
    raise AssertionError("turn did not end")


receipt = (
    json.loads(args.output.read_text())
    if args.resume
    else {"session_prefix": run_id, "warm_seconds": [], "miss_seconds": []}
)
for i in range(len(receipt["warm_seconds"]), 5):
    wait_warm()
    session = f"warm-{i}"
    receipt["warm_seconds"].append(bind(session))
    assert "-warm-" in sandbox(session)
    args.output.write_text(json.dumps(receipt, indent=2))
    kube("delete", "pod", sandbox(session), "--wait=true")

# Consume the ready pool, then immediately bind a second conversation.
wait_warm()
bind("pool-consumer")
for i in range(len(receipt["miss_seconds"]), 5):
    # Delete only our unassigned pool pods so these requests deliberately miss.
    for pod in pods():
        if pod["metadata"]["labels"]["daimon-session"] == "warm":
            kube("delete", "pod", pod["metadata"]["name"], "--wait=true")
    session = f"miss-{i}"
    receipt["miss_seconds"].append(bind(session))
    assert "-sh-" in sandbox(session)
    args.output.write_text(json.dumps(receipt, indent=2))
    kube("delete", "pod", sandbox(session), "--wait=true")

wait_warm()
receipt["write"] = turn("persistent", "write")
assert "POD_RESUME_OK" in json.dumps(receipt["write"])
# Wait until the actual archive is durable, not merely the terminal event.
kube(
    "exec",
    "deployment/" + name,
    "--",
    "python",
    "-c",
    'import pathlib,time; root=pathlib.Path("/checkpoints"); end=time.time()+60\nwhile not list(root.rglob("*.tar.zst")):\n assert time.time()<end; time.sleep(.2)',
)
old = sandbox("persistent")
kube("delete", "pod", old, "--wait=true")
start = time.monotonic()
receipt["read"] = turn("persistent", "read")
receipt["resume_seconds"] = time.monotonic() - start
assert "POD_RESUME_OK" in json.dumps(receipt["read"])
receipt["binding_before_restart"] = sandbox("persistent")
# Restart controller in a separate caller so the port-forward can reconnect externally.
receipt["restart_pending"] = True
args.output.write_text(json.dumps(receipt, indent=2))
print(json.dumps({k: v for k, v in receipt.items() if k.endswith("seconds")}, indent=2))

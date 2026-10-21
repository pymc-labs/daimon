"""Finish restart, actual idle reconciliation and OOM-isolation smoke checks."""

import argparse
import concurrent.futures
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from mux.drivers.selfhosted.transport import sandbox_id

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--deployment", default="smoke-daimon-harness")
args = parser.parse_args()
receipt = json.loads(args.output.read_text())
prefix = receipt["session_prefix"]
name = args.deployment
client = httpx.Client(
    base_url=args.url,
    headers={"Authorization": "Bearer " + os.environ.get("L16_SMOKE_TOKEN", "offline-l16")},
    timeout=200,
    limits=httpx.Limits(max_keepalive_connections=0),
)


def kube(*words):
    return subprocess.check_output(["kubectl", "-n", "daimon-pods", *words], text=True).strip()


def pods():
    return json.loads(kube("get", "pods", "-l", "daimon-controller=" + name, "-o", "json"))["items"]


def find(session):
    key = sandbox_id(prefix + session)
    return next(p for p in pods() if p["metadata"]["labels"]["daimon-session"] == key)


def turn(session, mode):
    session = prefix + session
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
    deadline = time.time() + 90
    while time.time() < deadline:
        response = client.get(
            f"/v0/sessions/{session}/events", params={"session_id": session, "after_seq": 0}
        )
        assert response.status_code == 200, response.text
        events = response.json()["events"]
        terminal = next(
            (e for e in events if e["turn_id"] == turn_id and e["type"] == "turn_ended"), None
        )
        if terminal:
            assert terminal["data"]["outcome"] == "completed", terminal
            return [e for e in events if e["turn_id"] == turn_id]
        time.sleep(0.1)
    raise AssertionError("turn did not complete")


# The shell restarted the controller and established a new port-forward first.
receipt["restart_binding_unchanged"] = (
    find("persistent")["metadata"]["name"] == receipt["binding_before_restart"]
)
assert receipt["restart_binding_unchanged"]
receipt["restart_read"] = turn("persistent", "read")
assert "POD_RESUME_OK" in json.dumps(receipt["restart_read"])
old = find("persistent")["metadata"]["name"]
kube("annotate", "pod", old, "daimon-last-active=0", "--overwrite=true")
start = time.monotonic()
while any(p["metadata"]["name"] == old for p in pods()):
    assert time.monotonic() - start < 120
    time.sleep(0.25)
receipt["reap_detection_seconds"] = time.monotonic() - start
start = time.monotonic()
receipt["reap_read"] = turn("persistent", "read")
receipt["resume_after_reap_seconds"] = time.monotonic() - start
assert "POD_RESUME_OK" in json.dumps(receipt["reap_read"])
# Establish both pods before provoking the OOM, so the survivor is independent.
turn("survivor", "hello")
survivor = find("survivor")["metadata"]["uid"]
turn("victim", "oom")
victim = find("victim")["metadata"]["name"]
with concurrent.futures.ThreadPoolExecutor() as workers:
    surviving = workers.submit(turn, "survivor", "hello")
    deadline = time.time() + 60
    reason = None
    while time.time() < deadline:
        current = next((p for p in pods() if p["metadata"]["name"] == victim), None)
        if current:
            reason = next(
                (
                    c.get("state", {}).get("terminated", {}).get("reason")
                    for c in current.get("status", {}).get("containerStatuses", [])
                    if c.get("state", {}).get("terminated")
                ),
                None,
            )
            if reason:
                break
        time.sleep(0.1)
    assert reason == "OOMKilled", reason
    receipt["oom_reason"] = reason
    receipt["survivor_events"] = surviving.result()
assert find("survivor")["metadata"]["uid"] == survivor
receipt["oom_survivor_uid_unchanged"] = True
receipt.pop("restart_pending", None)
args.output.write_text(json.dumps(receipt, indent=2))
print("restart / idle reap / workspace restore / OOM isolation: PASS")

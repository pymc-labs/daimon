"""Independent request-order checks; metadata replay does not certify native codecs."""

from pathlib import Path
from typing import Literal

from mux.conformance.recording import RecordingError, Replay, RequestMetadata

LIVE_IDS = frozenset({"C10", "C15", "C16"})
SESSION = "/v1/agents/sessions/resource-2"
AGENT = "/v1/agents/resource-1"


def expected(
    method: Literal["GET", "POST", "DELETE"], path: str, fields: tuple[str, ...] = ()
) -> RequestMetadata:
    headers = {"authorization": "[redacted]", "accept": "application/json"}
    if method != "GET":
        headers["content-type"] = "application/json"
    return RequestMetadata(method=method, path=path, headers=headers, body_fields=fields)


async def check(path: Path, *, legacy_cleanup: bool = False) -> dict[str, str | int]:
    replay = Replay.load(path)
    fixture = replay.tape.fixture_id
    if replay.tape.provider != "openai" or replay.tape.model != "gpt-6-luna":
        raise RecordingError("wrong provider or model")
    if fixture not in LIVE_IDS:
        if replay.tape.batches:
            raise RecordingError("pending fixture contains provider I/O")
        replay.finish()
        return {"fixture_id": fixture, "metadata_replay": "pass", "batches": 0, "events": 0}
    prefix = [
        expected("POST", "/v1/agents", ("name", "model", "metadata")),
        expected("GET", AGENT),
        expected("POST", "/v1/agents/sessions", ("agent_id", "environment", "metadata")),
    ]
    if fixture == "C15":
        prefix += [expected("GET", SESSION), expected("GET", SESSION)]
    if fixture == "C16":
        prefix += [
            expected("GET", SESSION + "/turns"),
            expected("GET", SESSION + "/items"),
            expected("GET", SESSION),
            expected("GET", SESSION + "/turns"),
            expected("GET", SESSION + "/items"),
        ]
    for request in prefix:
        if await replay.events(request):
            raise RecordingError("idle fixture unexpectedly emitted events")
    tail = replay.tape.batches[len(prefix) :]
    if not 2 <= len(tail) <= 63:
        raise RecordingError("cleanup request bound exceeded")
    if tail[-1].request != expected("DELETE", AGENT):
        raise RecordingError("agent cleanup is missing or out of order")
    if tail[-2].request != expected("DELETE", SESSION):
        raise RecordingError("session cleanup is missing or out of order")
    if not legacy_cleanup and tail[0].request != expected("GET", SESSION):
        raise RecordingError("driver cleanup did not inspect session readiness")
    previous = None
    for batch in tail[:-1]:
        request = batch.request
        if request.method == "GET":
            oracle = expected("GET", SESSION)
        elif request.method == "DELETE" and (legacy_cleanup or previous == "GET"):
            oracle = expected("DELETE", SESSION)
        else:
            raise RecordingError("unexpected cleanup mutation or delete without fresh read")
        if await replay.events(oracle):
            raise RecordingError("cleanup unexpectedly emitted events")
        previous = request.method
    if await replay.events(expected("DELETE", AGENT)):
        raise RecordingError("agent cleanup unexpectedly emitted events")
    replay.finish()
    return {
        "fixture_id": fixture,
        "metadata_replay": "pass",
        "batches": len(replay.tape.batches),
        "events": 0,
    }

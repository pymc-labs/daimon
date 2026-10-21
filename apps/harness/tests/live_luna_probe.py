"""Opt-in paid Luna HTTP probes. Run as root with HARNESS_LIVE_PROBE=openai.

The parent forks one isolated service per loop and saves only sanitized evidence.
No credential is accepted as a CLI argument, persisted, or inherited by tools.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import socket
import tempfile
from pathlib import Path

import httpx
import uvicorn
from daimon_harness.credentials import redact
from daimon_harness.isolation import protect_process
from daimon_harness.service import Supervisor, create_app
from mcp.server.fastmcp import FastMCP

MODEL = "gpt-6-luna"


class AuditStream(httpx.AsyncByteStream):
    def __init__(self, source, record, streaming):
        self.source, self.record, self.streaming = source, record, streaming
        self.buffer = b""
        self.all_bytes = bytearray()

    def capture(self, response):
        if response.get("usage") is not None:
            self.record["response_id"] = response.get("id")
            self.record["provider_model"] = response.get("model")
            self.record["raw_usage"] = response["usage"]
            self.record["output_items"] = [
                {key: item.get(key) for key in ["type", "name", "namespace"]}
                for item in response.get("output", [])
            ]

    async def __aiter__(self):
        async for chunk in self.source:
            if self.streaming:
                self.buffer += chunk
                while b"\n\n" in self.buffer:
                    block, self.buffer = self.buffer.split(b"\n\n", 1)
                    data = b"\n".join(
                        line[5:].lstrip()
                        for line in block.splitlines()
                        if line.startswith(b"data:")
                    )
                    if data and data != b"[DONE]":
                        event = json.loads(data)
                        self.capture(event.get("response", {}))
            else:
                self.all_bytes.extend(chunk)
            yield chunk
        if not self.streaming and self.all_bytes:
            self.capture(json.loads(self.all_bytes))

    async def aclose(self):
        await self.source.aclose()


class AuditTransport(httpx.AsyncBaseTransport):
    def __init__(self, state):
        self.inner = httpx.AsyncHTTPTransport()
        self.state = state

    async def handle_async_request(self, request):
        if request.url.host != "api.openai.com":
            return await self.inner.handle_async_request(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses" and body["model"] == MODEL
        supervisor = self.state["supervisor"]
        context = next(iter(supervisor.contexts.values()))
        events = await supervisor.io(supervisor.journal.events, context.request.session_id)
        starts = [
            e
            for e in events
            if e.turn_id == context.request.turn_id and e.type == "model_request_started"
        ]
        users = [item for item in body.get("input", []) if item.get("role") == "user"]
        private_paths_absent = context.state_dir is None or str(
            context.state_dir
        ) not in json.dumps(body)
        assert private_paths_absent
        observations = self.state["observations"]
        record = {
            "compaction_prompt": bool(
                len(users) > 1 and "id" not in users[-1] and body["input"][-1] == users[-1]
            ),
            "compaction_summary": any("id" in user for user in users),
            "private_paths_absent": private_paths_absent,
            "turn_id": context.request.turn_id,
            "request_id": starts[-1].data["request_id"],
            "started_seq_before_network": starts[-1].seq,
            "stream": body.get("stream", False),
        }
        assert not any(r["request_id"] == record["request_id"] for r in observations)
        observations.append(record)
        request.headers["Accept-Encoding"] = "identity"
        response = await self.inner.handle_async_request(request)
        record["http_status"] = response.status_code
        response.stream = AuditStream(response.stream, record, bool(body.get("stream")))
        return response

    async def aclose(self):
        await self.inner.aclose()


def listen():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    return listener, listener.getsockname()[1]


async def wait_started(server, task):
    async with asyncio.timeout(10):
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)


async def run_loop(
    home: Path, output: Path, backend: str, api_key: str, binary: str, scenario=None
):
    os.environ["HARNESS_LOOP"] = backend
    os.environ["HARNESS_LIVE_PROBE"] = "openai"
    os.environ["HARNESS_RUNTIME_MODE"] = "probe"
    os.environ["HARNESS_CODEX_BINARY"] = binary
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ.pop("HARNESS_TEST_MODE", None)
    os.environ["HARNESS_SESSION_MODE"] = "single"
    token = secrets.token_urlsafe(32)
    original_client = httpx.AsyncClient
    state = {"observations": []}

    class AuditedClient(original_client):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", AuditTransport(state))
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = AuditedClient
    from daimon_harness import model_adapter

    original_openai = model_adapter.AsyncOpenAI

    def audited_openai(**kwargs):
        return original_openai(**kwargs, http_client=AuditedClient())

    model_adapter.AsyncOpenAI = audited_openai
    # This is the real service constructor: credentials captured, UID dropped,
    # environment scrubbed, non-dumpable set, normal production checks retained.
    # This probe runs on a build host, not in the production container. Avoid
    # traversing unrelated Docker overlay mounts; inspect this private sandbox.
    # The production service retains its complete mount scan unchanged.
    from daimon_harness import service

    original_scan = service.validate_credential_files
    service.validate_credential_files = lambda **_: original_scan(roots=(home,), home=home)
    app = create_app(home=home, token=token)
    supervisor: Supervisor = app.state.supervisor
    original_run = supervisor.adapter.run

    async def diagnosed_run(context, history):
        try:
            return await original_run(context, history)
        except Exception as exc:
            import traceback

            print(
                json.dumps(
                    redact(
                        {
                            "adapter_failure": type(exc).__name__,
                            "cause": str(exc.__cause__) if exc.__cause__ else None,
                            "message": str(exc),
                            "frames": [
                                (Path(f.filename).name, f.name, f.lineno)
                                for f in traceback.extract_tb(exc.__traceback__)
                            ],
                        },
                        {"api_key": api_key, "token": token},
                    )
                ),
                flush=True,
            )
            raise

    supervisor.adapter.run = diagnosed_run
    state["supervisor"] = supervisor
    assert os.geteuid() == 10000 and "OPENAI_API_KEY" not in os.environ
    mcp = FastMCP("probe", stateless_http=True)
    executed = []

    @mcp.tool()
    def echo(text: str) -> str:
        """Return the supplied text with an echo prefix."""
        executed.append(text)
        return "echo:" + text

    mcp_listener, mcp_port = listen()
    mcp_server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="error"))
    mcp_task = asyncio.create_task(mcp_server.serve(sockets=[mcp_listener]))
    listener, port = listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    await wait_started(mcp_server, mcp_task)
    await wait_started(server, serving)
    from sse_starlette.sse import AppStatus

    AppStatus.enable_automatic_graceful_drain = False
    results = []
    try:
        async with original_client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=150,
            headers={"Authorization": "Bearer " + token},
        ) as client:
            for name in (
                [scenario]
                if scenario
                else [
                    "plain",
                    "mcp_auto",
                    "approve",
                    "deny",
                    "isolated_bash",
                    "multi_request",
                    "cancel",
                ]
            ):
                turn_id = backend + "-" + name
                policy = {tool: "deny" for tool in ["bash", "read", "write", "edit", "probe/*"]}
                mcp_servers = []
                if name == "plain":
                    prompt = "Reply with exactly: Luna streaming works."
                elif name in {"mcp_auto", "approve", "deny", "multi_request"}:
                    policy["probe/echo"] = "ask" if name in {"approve", "deny"} else "auto"
                    mcp_servers = [
                        {
                            "name": "probe",
                            "url": f"http://127.0.0.1:{mcp_port}/mcp",
                            "headers_env": {},
                        }
                    ]
                    prompt = (
                        'Call the available echo tool exactly once with text="luna-'
                        + name
                        + '". Then reply briefly with its result. If denied, report denied without retrying.'
                    )
                    if name == "multi_request":
                        prompt = 'Call the available echo tool with text="first". Wait for its result, then call the echo tool again with the returned string as text. After both results reply briefly. You must perform both calls sequentially.'
                elif name == "compaction":
                    policy["bash"] = "auto"
                    commands = [f"seq {i * 100000} {i * 100000 + 19999}" for i in range(1, 15)]
                    prompt = (
                        "Run these 14 commands in order, one bash tool call per command, "
                        "waiting for each result before the next. Run each command exactly once "
                        "without changing or shortening it. After all 14, report the first number "
                        "from each output in order. Keep track of completed commands if context "
                        "is compacted; never repeat a completed command.\n" + "\n".join(commands)
                    )
                elif name == "isolated_bash":
                    policy["bash"] = "auto"
                    prompt = "Call bash exactly once to execute this command, then report the result: python3 -c \"import os; print('tool_uid='+str(os.geteuid())); print('openai_key_visible='+str('OPENAI_API_KEY' in os.environ))\""
                else:
                    prompt = "Print integers 1 through 10000, each on a separate line, without skipping any. Start immediately. Do not use tools."
                request = {
                    "session_id": "luna-live",
                    "turn_id": turn_id,
                    "idempotency_key": turn_id,
                    "model": MODEL,
                    "instructions": "Follow the user request. Use only the provided tools. Do not retry denied calls. Keep final answers short unless asked for integers.",
                    "input": [{"type": "text", "text": prompt}],
                    "mcp_servers": mcp_servers,
                    "skills": [],
                    "tool_policy": policy,
                    "limits": {
                        "max_tool_calls": 22 if name == "compaction" else 5,
                        "deadline_s": 600 if name == "compaction" else 120,
                    },
                }
                events, errors = [], []
                count_before = len(executed)
                cancel_trigger = None
                try:
                    async with asyncio.timeout(620 if name == "compaction" else 140):
                        async with client.stream("POST", "/v0/turns", json=request) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                event = json.loads(line[5:])
                                events.append(event)
                                if event["type"] == "requires_action":
                                    action = await client.post(
                                        f"/v0/turns/{turn_id}/actions",
                                        json={
                                            "action_id": event["data"]["action_id"],
                                            "decision": "deny" if name == "deny" else "allow",
                                        },
                                    )
                                    action.raise_for_status()
                                cancel_now = (
                                    name == "cancel"
                                    and cancel_trigger is None
                                    and (
                                        (
                                            backend == "codex"
                                            and event["type"] == "agent_message_delta"
                                        )
                                        or (
                                            backend == "agents_sdk"
                                            and event["type"] == "model_request_started"
                                        )
                                    )
                                )
                                if cancel_now:
                                    cancel_trigger = event["type"]
                                    if backend == "agents_sdk":
                                        await asyncio.sleep(0.3)
                                    cancelled = await client.post(f"/v0/turns/{turn_id}/cancel")
                                    cancelled.raise_for_status()
                except Exception as exc:
                    errors.append(type(exc).__name__)
                # The journal is authoritative, including any suffix after reader loss.
                durable = await client.get("/v0/sessions/luna-live/events")
                durable.raise_for_status()
                events = [e for e in durable.json()["events"] if e["turn_id"] == turn_id]
                starts = [e for e in events if e["type"] == "model_request_started"]
                measured = [e for e in events if e["type"] == "usage"]
                unknown = [
                    e
                    for e in events
                    if e["type"] == "error" and e["data"].get("kind") == "usage_unavailable"
                ]
                observed = [r for r in state["observations"] if r["turn_id"] == turn_id]
                usage_checks = []
                for start in starts:
                    identity = start["data"]["request_id"]
                    endings = [e for e in measured + unknown if e["data"]["request_id"] == identity]
                    assert len(endings) == 1 and start["seq"] < endings[0]["seq"]
                    raw = next(r for r in observed if r["request_id"] == identity)
                    if endings[0]["type"] == "usage":
                        provider = raw["raw_usage"]
                        expected = {
                            "input_tokens": provider["input_tokens"],
                            "output_tokens": provider["output_tokens"],
                            "cached_input_tokens": provider.get("input_tokens_details", {}).get(
                                "cached_tokens", 0
                            ),
                            "reasoning_tokens": provider.get("output_tokens_details", {}).get(
                                "reasoning_tokens", 0
                            ),
                        }
                        assert all(
                            endings[0]["data"][key] == value for key, value in expected.items()
                        )
                    usage_checks.append(
                        {
                            "request_id": identity,
                            "exactly_one_outcome": True,
                            "started_before_network_and_observation": True,
                            "provider_counts_match": endings[0]["type"] == "usage",
                        }
                    )
                outcome = (
                    events[-1]["data"].get("outcome")
                    if events and events[-1]["type"] == "turn_ended"
                    else None
                )
                deltas = [e for e in events if e["type"] == "agent_message_delta"]
                finals = [e for e in events if e["type"] == "agent_message"]
                if name != "cancel":
                    if outcome != "completed":
                        errors.append("turn_not_completed")
                    if not deltas or not finals:
                        errors.append("missing_message_stream")
                    for final in finals:
                        message_deltas = [
                            e
                            for e in deltas
                            if e["data"]["message_id"] == final["data"]["message_id"]
                        ]
                        if not message_deltas:
                            errors.append("message_id_mismatch")
                else:
                    if outcome != "interrupted":
                        errors.append("cancel_not_interrupted")
                    if cancel_trigger is None:
                        errors.append("cancel_not_triggered")
                if name in {"approve", "deny"} and not any(
                    e["type"] == "requires_action" for e in events
                ):
                    errors.append("missing_approval")
                if name in {"mcp_auto", "approve"} and len(executed) != count_before + 1:
                    errors.append("tool_execution_count")
                if name == "deny" and len(executed) != count_before:
                    errors.append("denied_tool_executed")
                if name == "multi_request" and (
                    len(starts) < 2 or len(executed) != count_before + 2
                ):
                    errors.append("multi_request_not_observed")
                if name == "isolated_bash":
                    tool_result = str([e["data"] for e in events if e["type"] == "tool_result"])
                    if (
                        "tool_uid=10001" not in tool_result
                        or "openai_key_visible=False" not in tool_result
                    ):
                        errors.append("tool_isolation_not_proven")
                compaction_evidence = {}
                if name == "compaction":
                    actual = [
                        e["data"]["input"]["command"] for e in events if e["type"] == "tool_use"
                    ]
                    if actual != commands:
                        errors.append("commands_repeated_or_missing")
                    if not any(r["compaction_prompt"] for r in observed) or not any(
                        r["compaction_summary"] for r in observed
                    ):
                        errors.append("compaction_not_observed")
                    history = supervisor.journal.history("luna-live")
                    # Persist a compact transcript with output digests rather than
                    # megabytes of seq output. Inventory and usage remain exact.
                    compaction_evidence = {
                        "commands": actual,
                        "history_private_paths_absent": str(home) not in json.dumps(history),
                    }
                    if not compaction_evidence["history_private_paths_absent"]:
                        errors.append("private_path_in_history")
                    for event in events:
                        if event["type"] == "tool_result":
                            output_text = str(event["data"]["output"])
                            event["data"]["output"] = {
                                "excerpt": output_text[:100],
                                "characters": len(output_text),
                                "sha256": hashlib.sha256(output_text.encode()).hexdigest(),
                            }
                result = {
                    **compaction_evidence,
                    "backend": backend,
                    "scenario": name,
                    "model": MODEL,
                    "service_uid": os.geteuid(),
                    "outcome": outcome,
                    "delta_count": len(deltas),
                    "cancel_trigger": cancel_trigger,
                    "errors": errors,
                    "usage_checks": usage_checks,
                    "api_observations": observed,
                    "events": events,
                }
                sanitized = redact(result, {"api_key": api_key, "harness_token": token})
                encoded = json.dumps(sanitized, indent=2)
                assert api_key not in encoded and token not in encoded
                (output / (turn_id + ".json")).write_text(encoded + "\n")
                summary = {
                    "backend": backend,
                    "scenario": name,
                    "outcome": outcome,
                    "requests": len(starts),
                    "measured": len(measured),
                    "unknown": len(unknown),
                    "deltas": len(deltas),
                    "errors": errors,
                }
                results.append(summary)
                print(json.dumps(summary), flush=True)
        return results
    except BaseException as exc:
        print(
            json.dumps(
                redact(
                    {"loop_failure": type(exc).__name__, "message": str(exc)},
                    {"api_key": api_key, "token": token},
                )
            ),
            flush=True,
        )
        raise
    finally:
        server.should_exit = True
        mcp_server.should_exit = True
        await asyncio.wait_for(asyncio.gather(serving, mcp_task), 15)
        listener.close()
        mcp_listener.close()
        httpx.AsyncClient = original_client
        model_adapter.AsyncOpenAI = original_openai


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--backend", choices=["codex", "agents_sdk"])
    parser.add_argument("--scenario", choices=["compaction"])
    args = parser.parse_args()
    assert os.geteuid() == 0, "probe needs real service/tool UID separation"
    assert os.environ.get("HARNESS_LIVE_PROBE") == "openai", "explicit probe opt-in required"
    assert os.environ.get("AZURE_OPENAI_DEPLOYMENT", MODEL) == MODEL
    api_key = os.environ["OPENAI_API_KEY"]
    protect_process()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="luna-live-") as scratch:
        root = Path(scratch)
        root.chmod(0o711)
        failures = 0
        for backend in [args.backend] if args.backend else ["codex", "agents_sdk"]:
            home, output = root / backend, root / (backend + "-results")
            home.mkdir()
            output.mkdir()
            output.chmod(0o700)
            os.chown(output, 10000, 10000)
            child = os.fork()
            if child == 0:
                try:
                    results = asyncio.run(
                        run_loop(home, output, backend, api_key, args.binary, args.scenario)
                    )
                    if any(result["errors"] for result in results):
                        os._exit(1)
                except BaseException as exc:
                    # Print only the exception class; traceback/provider bodies may leak.
                    import traceback

                    print(
                        json.dumps(
                            {
                                "backend": backend,
                                "probe_failure": type(exc).__name__,
                                "frames": [
                                    (Path(f.filename).name, f.name, f.lineno)
                                    for f in traceback.extract_tb(exc.__traceback__)
                                ],
                            }
                        ),
                        flush=True,
                    )
                    os._exit(1)
                os._exit(0)
            _, status = os.waitpid(child, 0)
            for path in output.iterdir():
                destination = args.output / path.name
                shutil.copyfile(path, destination)
                if "SUDO_UID" in os.environ:
                    os.chown(destination, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
            if status:
                failures += 1
                print(json.dumps({"backend": backend, "worker_status": status}), flush=True)
        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

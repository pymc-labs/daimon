"""Offline cluster fixture only; never mounted by the production chart."""

import os
from pathlib import Path

import uvicorn
from daimon_harness.isolation import prepare_service, protect_process, validate_session_mode
from daimon_harness.service import create_app


class FakeModelAdapter:
    async def run(self, context, history):
        counter = Path.home() / "executions"
        with counter.open("a") as output:
            output.write(context.request.turn_id + "\n")
        request_id = "fake-" + context.request.turn_id
        await context.emit(
            "model_request_started", {"request_id": request_id, "model": context.request.model}
        )

        async def write():
            proof = await context.bash(
                'test -z "${HARNESS_TOKEN:-}" && test "$(id -u)" -ge 10001 && echo TOOL_ISOLATED'
            )
            assert "TOOL_ISOLATED" in proof, proof
            if context.request.input[0].text == "read":
                result = context.read("kind-proof.txt")
                assert "persisted" in str(result), result
                return result
            return context.write("kind-proof.txt", "persisted")

        await context.tool("write-1", "write", {"path": "kind-proof.txt"}, write)
        await context.emit(
            "usage",
            dict(
                request_id=request_id,
                model=context.request.model,
                input_tokens=10,
                cached_input_tokens=0,
                output_tokens=4,
                reasoning_tokens=0,
            ),
        )
        await context.emit(
            "agent_message",
            {"message_id": context.request.turn_id, "parts": [{"type": "text", "text": "done"}]},
        )
        return [*history, {"role": "assistant", "content": "done"}]


# The host runtime injects readable synthetic DMI bind mounts into kind pods.
# Use the documented offline mode for credential-file scanning only; explicitly
# verify multi isolation and apply the production privilege drop/protection.
token = os.environ.pop("HARNESS_TOKEN")
validate_session_mode()
os.environ["HARNESS_TEST_MODE"] = "1"
app = create_app(token=token, adapter=FakeModelAdapter())
prepare_service(Path.home())
protect_process()
assert os.geteuid() == 10000
with open("/proc/self/status") as status:
    capabilities = next(line.split()[1] for line in status if line.startswith("CapBnd:"))
assert int(capabilities, 16) & (1 << 21) == 0, "SYS_ADMIN remains regainable in bounding set"
print("Production startup verified: service UID 10000; SYS_ADMIN dropped", flush=True)
uvicorn.run(app, host="0.0.0.0", port=8088)

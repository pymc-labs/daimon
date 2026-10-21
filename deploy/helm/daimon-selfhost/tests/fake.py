"""Offline cluster fixture only; never mounted by the production chart."""

import os
from pathlib import Path

from daimon_harness.isolation import validate_credential_files
from daimon_harness.model_adapter import bound_mcp_http_client
from daimon_harness.service import create_app as service_create_app


class FakeModelAdapter:
    async def run(self, context, history):
        assert os.geteuid() == 10000
        assert not os.environ.get("HARNESS_TEST_MODE")
        async with bound_mcp_http_client() as client:
            response = await client.get(
                os.environ["HARNESS_DAIMON_MCP_URL"].removesuffix("/mcp") + "/healthz"
            )
            assert response.status_code == 200
        print("F80: service MCP client read CA after production UID drop", flush=True)
        counter = Path.home() / "executions"
        counter.touch(mode=0o600, exist_ok=True)
        with counter.open("a") as output:
            output.write(context.request.turn_id + "\n")
        request_id = "fake-" + context.request.turn_id
        await context.emit(
            "model_request_started", {"request_id": request_id, "model": context.request.model}
        )

        async def write():
            proof = await context.bash(
                'test -z "${HARNESS_TOKEN:-}" && test "$(id -u)" -ge 10001 && test ! -r /etc/daimon/harness-tls/tls.key && test ! -r /etc/daimon/mcp-ca/ca.crt && echo TOOL_ISOLATED'
            )
            assert "TOOL_ISOLATED" in proof, proof
            if context.request.input[0].text == "read":
                result = context.read("kind-proof.txt")
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


# Replace inference only; all production startup checks and privilege changes run.
def create_app():
    # The production Uvicorn command loads SSL before invoking this factory.
    key = Path("/etc/daimon/harness-tls/tls.key")
    assert key.stat().st_uid == 0 and key.stat().st_mode & 0o777 == 0o400
    assert not os.environ.get("HARNESS_TEST_MODE"), "production startup required"
    ca = Path(os.environ["HARNESS_MCP_CA_BUNDLE"])
    validate_credential_files(roots=(key.parent, ca.parent), home=Path.home())
    print("F80: production credential-file scan accepts chart CA/TLS mounts", flush=True)
    app = service_create_app(adapter=FakeModelAdapter())
    assert ca.read_bytes(), "service cannot read chart CA after UID drop"
    assert os.geteuid() == 10000
    try:
        key.read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError("TLS key readable after service UID drop")
    with open("/proc/self/status") as status:
        capabilities = next(line.split()[1] for line in status if line.startswith("CapBnd:"))
    assert int(capabilities, 16) & (1 << 21) == 0, "SYS_ADMIN remains regainable in bounding set"
    print(
        "Production TLS startup verified: UID 10000; root key unreadable; SYS_ADMIN dropped",
        flush=True,
    )
    return app

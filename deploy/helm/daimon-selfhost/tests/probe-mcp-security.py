"""Inside MCP as the operator UID: verify the running TLS service boundary."""

import os
from pathlib import Path

assert os.getuid() == os.getgid() == 1000
for process in ("1", "self"):
    status = dict(
        line.split(":", 1) for line in Path(f"/proc/{process}/status").read_text().splitlines()
    )
    assert {int(n) for n in status["Uid"].split()} == {1000}, status["Uid"]
    assert int(status["CapEff"], 16) == int(status["CapPrm"], 16) == 0
    assert int(status["CapBnd"], 16) == (1 << 6) | (1 << 7)
    assert int(status["NoNewPrivs"]) == 1
    assert int(status["Seccomp"]) == 2
try:
    Path("/etc/daimon/mcp-tls/tls.key").read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError("MCP app/operator UID can read the root TLS key")
print(
    "MCP service and operator UID 1000; empty effective capabilities, NNP, seccomp, key unreadable"
)

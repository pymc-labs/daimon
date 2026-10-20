"""Probe startup isolation in a fresh Linux process, with synthetic secrets only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process sandbox")
def test_tools_cannot_read_startup_credentials_or_harness_proc(tmp_path: Path) -> None:
    script = tmp_path / "probe.py"
    script.write_text("""
import asyncio
import os
from pathlib import Path
from daimon_harness.service import create_app
from daimon_harness.runtime import TurnContext, discard_event
from mux.drivers.selfhosted.wire import TurnRequest, InlineSkill

secrets = [os.environ[name] for name in ("AZURE_OPENAI_API_KEY", "HARNESS_TOKEN", "HARNESS_MCP_CRED_TEST")]
app = create_app(home=Path(os.environ["PROBE_HOME"]))
assert app.state.supervisor.adapter.key == secrets[0]
assert app.state.supervisor.operator_credentials["HARNESS_MCP_CRED_TEST"] == secrets[2]
assert not any(name in os.environ for name in ("AZURE_OPENAI_API_KEY", "HARNESS_TOKEN", "HARNESS_MCP_CRED_TEST"))
request = TurnRequest(session_id="s", turn_id="t", idempotency_key="k", model="m", instructions="", input=[], mcp_servers=[], skills=[], limits={"max_tool_calls": 1, "deadline_s": 5})
context = TurnContext(request, Path(os.environ["PROBE_HOME"]), discard_event)
command = "env; cat /proc/%d/environ 2>/dev/null; cat /proc/%d/cmdline 2>/dev/null; dd if=/proc/%d/mem of=/dev/null bs=1 count=1 2>/dev/null || echo memory-protected; echo writable > allowed.txt" % (os.getpid(), os.getpid(), os.getpid())
request.skills = [InlineSkill(name="analysis", files={"SKILL.md": "readable-skill"})]
asyncio.run(context.skills())
command += '; cat "$HOME/../skills/analysis/SKILL.md"; id -u'
output = asyncio.run(context.bash(command))
assert all(secret not in output for secret in secrets)
assert "memory-protected" in output, output
assert "readable-skill" in output, output
if os.geteuid() == 0:
    assert "10001" in output, output
assert (context.work / "allowed.txt").read_text().strip() == "writable"
app.state.supervisor.journal.close()
""")
    env = {
        name: value for name, value in os.environ.items() if name in {"PATH", "PYTHONPATH", "HOME"}
    }
    env.update(
        AZURE_OPENAI_API_KEY="synthetic-model-secret",
        HARNESS_TOKEN="synthetic-harness-secret",
        HARNESS_MCP_CRED_TEST="synthetic-mcp-secret",
        PROBE_HOME=str(tmp_path / "sandbox"),
    )
    completed = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr

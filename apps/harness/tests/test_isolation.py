"""Probe startup isolation in a fresh Linux process, with synthetic secrets only."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="root Linux process sandbox; image CI always exercises it",
)
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


@pytest.mark.parametrize("hidepid", ["0", "1", "2", "invisible"])
@pytest.mark.parametrize("exempt_group", [False, True])
def test_proc_mount_requires_hidden_pids_without_exempt_group(tmp_path, hidepid, exempt_group):
    from daimon_harness.isolation import proc_hides_other_users

    mountinfo = tmp_path / "mountinfo"
    options = f"rw,hidepid={hidepid}" + (",gid=10001" if exempt_group else "")
    mountinfo.write_text(f"25 24 0:22 / /proc rw,nosuid - proc proc {options}\n")
    assert proc_hides_other_users(mountinfo) == (hidepid in {"2", "invisible"} and not exempt_group)


def test_multi_mode_refuses_unsafe_proc_tmp_and_missing_uid_switch(tmp_path, monkeypatch):
    from daimon_harness import isolation

    monkeypatch.delenv("HARNESS_TEST_MODE")
    monkeypatch.setenv("HARNESS_SESSION_MODE", "multi")
    monkeypatch.setattr(isolation, "can_switch_uid", lambda: True)
    mountinfo = tmp_path / "mountinfo"
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o755)
    mountinfo.write_text("25 24 0:22 / /proc rw - proc proc rw\n")
    with pytest.raises(RuntimeError, match="hidepid"):
        isolation.validate_session_mode(mountinfo=mountinfo, shared_tmp=scratch)
    mountinfo.write_text("25 24 0:22 / /proc rw - proc proc rw,hidepid=2\n")
    scratch.chmod(0o1777)
    with pytest.raises(RuntimeError, match="/tmp not writable"):
        isolation.validate_session_mode(mountinfo=mountinfo, shared_tmp=scratch)
    scratch.chmod(0o755)
    assert isolation.validate_session_mode(mountinfo=mountinfo, shared_tmp=scratch) == "multi"
    monkeypatch.setattr(isolation, "can_switch_uid", lambda: False)
    with pytest.raises(RuntimeError, match="UID switching"):
        isolation.validate_session_mode(mountinfo=mountinfo, shared_tmp=scratch)


def test_multi_app_refuses_startup_without_verified_isolation(tmp_path, monkeypatch):
    from daimon_harness import isolation
    from daimon_harness.service import create_app

    from .conftest import FakeModelAdapter

    monkeypatch.delenv("HARNESS_TEST_MODE")
    monkeypatch.setenv("HARNESS_SESSION_MODE", "multi")
    monkeypatch.setattr(isolation, "can_switch_uid", lambda: True)
    monkeypatch.setattr(isolation, "proc_hides_other_users", lambda path: False)
    monkeypatch.setattr("daimon_harness.service.can_switch_uid", lambda: True)
    with pytest.raises(RuntimeError, match="hidepid"):
        create_app(home=tmp_path, token="test", adapter=FakeModelAdapter())
    assert not (tmp_path / ".daimon").exists()

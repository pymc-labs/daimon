"""Entry points of the eval pod image (`python -m daimon_eval_sandbox <role>`).

- `net` (Docker): apply the packet rules, then run the gateway as its own uid.
- `init` (Kubernetes init container): apply the packet rules.
- `gateway`: the transparent proxy and fake resolver.
- `main`: wait for files and the gateway, trust the eval CA, then run the executor.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Final, cast

from daimon_eval_sandbox import netinit
from daimon_eval_sandbox.gateway import AGENT_PORT, DNS_PORT, EXEC_PORT, EXEC_UID, GATEWAY_UID

GATEWAY_DIR: Final = Path("/run/eval/gateway")
MAIN_DIR: Final = Path("/run/eval/main")
STATE_DIR: Final = Path("/run/eval/state")
WORKSPACE: Final = "/workspace"
AGENT_BIN: Final = "/opt/eval/agent-bin"
CA_FILE: Final = "/etc/ssl/certs/ca-certificates.crt"
CA_VARS: Final = {
    name: CA_FILE
    for name in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
    )
}
SYSTEM_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
#: The executor finds the agent wrapper first, as `bash` and as its shell.
EXEC_ENV: Final = {
    "PATH": f"{AGENT_BIN}:/opt/venv/bin:{SYSTEM_PATH}",
    "LANG": "C.UTF-8",
    "SHELL": f"{AGENT_BIN}/bash",
    **CA_VARS,
}


def _drop(uid: int, *, groups: str, bounding: str, no_new_privs: bool = False) -> list[str]:
    return [
        "setpriv",
        f"--reuid={uid}",
        f"--regid={uid}",
        f"--groups={groups}" if groups else "--clear-groups",
        "--inh-caps=-all",
        f"--bounding-set={bounding}",
        *(["--no-new-privs"] if no_new_privs else []),
        "--",
    ]


def _gateway() -> None:
    from mitmproxy import certs, options
    from mitmproxy.tools.dump import DumpMaster

    from daimon_eval_sandbox.gateway import Gateway, GatewayConfig

    # Leaf certificates start decades back, so tools on a fake past clock accept them.
    certs.CERT_VALIDITY_OFFSET = timedelta(days=-365 * 30)
    certs.CERT_EXPIRY = timedelta(days=365 * 31)  # counted from that offset
    confdir = Path(tempfile.mkdtemp(prefix="mitm-"))
    (confdir / "mitmproxy-ca.pem").write_bytes((GATEWAY_DIR / "ca.pem").read_bytes())
    config = GatewayConfig.load(GATEWAY_DIR / "config.json")

    async def run() -> None:
        modes = [
            f"transparent@127.0.0.1:{AGENT_PORT}",
            f"transparent@127.0.0.1:{EXEC_PORT}",
            f"dns@127.0.0.1:{DNS_PORT}",
        ]
        opts = options.Options(confdir=str(confdir), mode=modes)
        master = DumpMaster(opts, with_termlog=True, with_dumper=False)
        # An addon option, known once the addons load.
        opts.update(connection_strategy="lazy")  # pyright: ignore[reportUnknownMemberType]
        # The gateway answers every name itself; the stock resolver would look them up.
        master.addons.remove(master.addons.get("dnsresolver"))  # pyright: ignore[reportUnknownMemberType]
        master.addons.add(Gateway(config))  # pyright: ignore[reportUnknownMemberType]
        await master.run()

    asyncio.run(run())


def _wait(deadline_s: float = 300) -> None:
    end = time.monotonic() + deadline_s
    while not (STATE_DIR / "ready").exists():
        if time.monotonic() > end:
            raise SystemExit("pod files never arrived")
        time.sleep(0.2)
    while True:
        try:
            socket.create_connection(("127.0.0.1", AGENT_PORT), timeout=1).close()
            return
        except OSError:
            if time.monotonic() > end:
                raise SystemExit("gateway never came up") from None
            time.sleep(0.2)


def _strings(value: object) -> dict[str, str]:
    return (
        {str(k): str(v) for k, v in cast(dict[str, object], value).items()}
        if isinstance(value, dict)
        else {}
    )


def _agent_profile(pod: dict[str, object]) -> str:
    env = {
        "PATH": SYSTEM_PATH,
        "LANG": "C.UTF-8",
        "SHELL": "/bin/bash",
        "HOME": "/home/agent",
        "USER": "agent",
        **CA_VARS,
    }
    env.update(_strings(pod.get("agent_env")))
    clock = pod.get("clock")
    libs = glob.glob("/usr/lib/*/faketime/libfaketime.so.1")
    if clock:
        if not libs:
            raise SystemExit("a case clock needs libfaketime")
        env |= {"LD_PRELOAD": libs[0], "FAKETIME": f"@{clock}", "FAKETIME_DONT_RESET": "1"}
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
    return f"umask 002\n{exports}"


def executor_argv(executor: str, env: dict[str, str]) -> list[str]:
    python = [sys.executable, "-m"]
    match executor:
        case "anthropic":
            return [*python, "daimon_eval_sandbox", "anthropic-worker"]
        case "openai":
            remote, env_id = env["CODEX_REMOTE_URL"], env["CODEX_ENVIRONMENT_ID"]
            return ["codex", "exec-server", "--remote", remote, "--environment-id", env_id]
        case "gemini":
            return [
                "gemini-api",
                "environments",
                "connect",
                "--root-dir",
                WORKSPACE,
                "--workdir",
                WORKSPACE,
            ]
        case "selfhosted":
            return [*python, "daimon_harness"]
        case "probe":
            return ["sleep", "infinity"]
        case _:
            raise SystemExit(f"unknown executor {executor!r}")


def _main() -> None:
    _wait()
    ca = MAIN_DIR / "ca.crt"
    Path("/usr/local/share/ca-certificates/daimon-eval.crt").write_bytes(ca.read_bytes())
    subprocess.run(["update-ca-certificates"], check=True, capture_output=True)
    pod = json.loads((MAIN_DIR / "pod.json").read_text())
    profile = STATE_DIR / "agent.sh"
    profile.write_text(_agent_profile(pod))
    profile.chmod(0o644)
    executor = str(pod["executor"])
    env = {**EXEC_ENV, **_strings(pod.get("exec_env"))}
    argv = executor_argv(executor, env)
    if executor == "selfhosted":  # the harness runs as root and drops each tool to its own uid
        env |= {"HOME": "/root"}
        command = argv
    else:
        env |= {"HOME": "/home/exec", "USER": "exec"}
        # setuid/setgid stay in the bounding set only so `agent-exec` (file caps) can drop to agent.
        command = [*_drop(EXEC_UID, groups="agent", bounding="-all,+setuid,+setgid"), *argv]
    os.umask(0o002)
    sys.exit(subprocess.run(command, env=env, cwd=WORKSPACE).returncode)


def manifest() -> dict[str, list[str]]:
    """The agent-visible packages, for the parity check against the environment definition."""
    apt = subprocess.run(
        ["dpkg-query", "-W", "-f", "${Package}\\n"], capture_output=True, text=True, check=True
    )
    pip = subprocess.run(
        ["/usr/local/bin/python3", "-m", "pip", "list", "--format=json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        "apt": sorted(apt.stdout.split()),
        "pip": sorted(str(p["name"]).lower() for p in json.loads(pip.stdout)),
    }


def main() -> None:
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role == "net":
        netinit.apply()
        gateway = [sys.executable, "-m", "daimon_eval_sandbox", "gateway"]
        os.execvp(
            "setpriv",
            [*_drop(GATEWAY_UID, groups="", bounding="-all", no_new_privs=True), *gateway],
        )
    elif role == "init":
        netinit.apply()
    elif role == "gateway":
        _gateway()
    elif role == "main":
        _main()
    elif role == "anthropic-worker":
        from mux.drivers.anthropic.environment_worker import run

        asyncio.run(run())
    elif role == "manifest":
        print(json.dumps(manifest()))
    elif role == "probe":
        from daimon_eval_sandbox.probes import run_probes

        print(json.dumps(run_probes(sys.argv[2:])))
    else:
        raise SystemExit(f"unknown role {role!r}")


if __name__ == "__main__":
    main()

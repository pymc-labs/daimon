import json
import os
import subprocess
from pathlib import Path

os.environ["KUBECONFIG"] = os.path.expanduser("~/.kube/daimon-aks")
p = Path("/tmp/l15-first-turn.4QeXQP")
results = []
for pod in ("coke-daimon-harness-0", "deploy/coke-daimon-mcp"):
    base = ["kubectl", "exec", "-n", "daimon", pod, "--"]
    target = subprocess.check_output(base + ["mktemp", "-d"], text=True).strip()
    with (p / "curl.tar").open("rb") as f:
        subprocess.run(
            ["kubectl", "exec", "-i", "-n", "daimon", pod, "--", "tar", "xf", "-", "-C", target],
            stdin=f,
            check=True,
        )
    cmd = base + [
        target + "/curl/ld-linux-x86-64.so.2",
        "--library-path",
        target + "/curl/lib",
        target + "/curl/curl",
        "--noproxy",
        "*",
        "--connect-timeout",
        "5",
        "--max-time",
        "8",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "HTTP=%{http_code}\n",
        "https://api.anthropic.com",
    ]
    result = subprocess.run(cmd, text=True, capture_output=True)
    results.append(
        dict(
            pod=pod,
            command=cmd,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    )
    print(json.dumps(results[-1]))
(p / "curl-results.json").write_text(json.dumps(results, indent=2))

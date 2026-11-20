# daimon-eval-sandbox

The image an eval item runs in. One pod per item, three uids:

| uid | runs | network |
| --- | --- | --- |
| `exec` (10000; root for the harness) | the executor: Anthropic environment worker, `codex exec-server`, `gemini-api environments connect`, or the harness | control-plane hosts tunnelled untouched; nothing else |
| `agent` (10001 and up) | every command the agent runs | the eval world only |
| `gateway` (61000) | mitmproxy with `gateway.py` and a fake resolver | the world, control-plane and package-registry hosts |

`netinit.py` sets the packet rules (`init` on Kubernetes, `net` on Docker). Web ports go to
the gateway, DNS to its resolver, and every other packet is rejected. The executor's shells
start through `/opt/eval/agent-bin/bash`, which drops to the agent uid with the agent
environment only (`image/agent-exec.c`). Pod files arrive under `/run/eval`: the gateway's
`config.json` and `ca.pem`, and the main container's `pod.json` and `ca.crt`.

Probes: `python -m daimon_eval_sandbox probe [name ...]` prints pass, fail or skipped per tool.

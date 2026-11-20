# daimon-eval-sandbox

The image an eval item runs in. One pod per item, three uids:

| uid | runs | network |
| --- | --- | --- |
| `exec` (10000; root for the harness) | the executor: Anthropic environment worker, `codex exec-server`, `gemini-api environments connect`, or the harness | control-plane hosts tunnelled untouched; nothing else |
| `agent` (10001 and up) | every command the agent runs | the eval world only |
| `gateway` (61000) | mitmproxy with `gateway.py` and a fake resolver | DNS, the world, and public web ports for control-plane and allowed registry hosts |

`netinit.py` sets the packet rules (`init` on Kubernetes, `net` on Docker). Web ports go to
the gateway, DNS to its resolver, and every other packet is rejected; new inbound
connections reach only the harness port. The gateway opens no connection but a tunnel or
an allowed registry. Every shell the executor starts (`bash`, `/bin/sh`, its login shell,
`$SHELL`) goes through `image/exec-shell.c` to `image/agent-exec.c`, which drops to the
agent uid with the agent environment only. Executor binaries are unreadable to it, so
nothing they start can read their memory or environment. `probe isolation`, run as the
executor, checks each path. Pod files arrive under `/run/eval`: the gateway's
`config.json` and `ca.pem`, and the main container's `pod.json` and `ca.crt`.

Probes: `python -m daimon_eval_sandbox probe [name ...]` prints pass, fail or skipped per tool.

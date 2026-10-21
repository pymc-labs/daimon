Validated 2026-10-21 on kind 1.35 and the existing AKS 1.36 cluster, in a
separate `daimon-pods` namespace. Kind cluster `daimon-l16-20261021` was deleted
by exact name. The existing `daimon` namespace was not modified.

First authenticated C3 event poll for a new session, five samples each;
empirical p95 is the largest of these small samples, not a production SLO:

| Environment | Warm p50 / p95 | Miss p50 / p95 | Resume after pod deletion | Resume after idle reap |
| --- | --- | --- | --- | --- |
| kind | 0.175 / 0.473 s | 14.172 / 15.454 s | 16.184 s | 0.451 s |
| AKS | 0.599 / 0.644 s | 13.121 / 13.273 s | 14.084 s | 1.531 s |

Warm hits were checked against warm pod names; misses used deterministic cold
pod names after removing unassigned pool pods. Timing includes routing,
readiness and cold restore, with a cached image. It excludes inference. The
AKS runner resumed its saved receipt after a client keepalive disconnect;
subsequent calls disabled HTTP connection reuse. No controller restart or
product failure accompanied that disconnect.

Both environments passed production UID separation with fake model execution,
file write/read after pod loss, controller restart retaining the original
binding, actual idle reconciliation after expiring the persisted timestamp,
and a deliberate `OOMKilled` victim while another conversation completed in
its unchanged pod. Reap detection was 5.381 s on kind and 11.168 s on AKS
(including deletion). AKS victim restore reported interruption and unknown
usage without re-execution. Default idle TTL remained 900 seconds.

AKS NetworkPolicy checks, executed after kind:

- Tool UID: IMDS:80 and unapproved external HTTPS blocked; AOAI /32:443 reachable.
- Unrelated pod: sandbox ingress blocked, controller reachable; target remained Running.
- Service UID 10000, seccomp filtering active, SYS_ADMIN absent from bounding set,
  no hidepid remount, no projected Kubernetes token.
- Read-only MCP CA bundle: service UID can read; tool UID receives PermissionError.

The CA probe used a public synthetic CA. Verified HTTPS MCP integration depends
on the C3.9a changes in #1276 and lane 14's generated-CA/TLS chart integration.
It is not claimed as a live MCP tool result. Per-turn MCP credential forwarding
over the HTTP sandbox hop is currently refused pending that TLS boundary.

One real Azure turn used only `gpt-5.4-mini`, the C12.2 stand-in. It returned
`PODS_OK` in 4.042 s, with one measured request: 202 input / 0 cached / 7 output /
0 reasoning tokens. Deleting that sandbox and replaying restored exactly the
original events, one model request and one usage record; no second model call.
All benchmarks and isolation tests used the fake adapter.

The shared D2s_v6 had little unreserved CPU. This parallel test used controller
and sandbox CPU requests of 10m, sandbox memory request 128Mi, warm pool 1;
limits stayed controller 500m/384Mi and sandbox 750m/768Mi. These are smoke
requests, not a production sizing recommendation. The chart defaults are higher.

Validation: 100 checkpoint/recovery tests passed on PVC writes, including the
100-example recovery property. All ten isolated mutations were detected with
the expected assertions (the tenth ignores sequence reservation recovery).
The default CI property now uses PVC writes. The remaining harness suite passed
262 tests with 3 skipped, plus focused controller/auth checks after final guards.
Whole-tree pyright, Ruff/format and all 14 import contracts passed.

The Helm overlay applies to #1273 at
`ca82432b60fcb5e2587d770ac2f0da7e59df51d0`, with semantic renders for shared,
perConversation, explicit HTTP consent, required CA, tokenless sandbox security
and namespaced RBAC. MERGE HOLD: base `azure-selfhost`, no merge.

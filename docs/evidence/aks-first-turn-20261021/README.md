# AKS first turn — 21 October 2026

Two real production-path turns completed at 11:59:42–12:00:06 UTC against
Azure deployment **gpt-5.4-mini**, the authorized C12.2 stand-in. The first ran
bash and wrote a file; the second read it in the same session. Four AOAI
requests produced four measured C3 usage events, four Postgres usage rows and
four ledger debits. **The initial host egress gap was closed at 12:19 UTC**, as documented in the
lockdown follow-up below. The original evidence is retained for comparison. This is implementation evidence for rubric C,
not an independent rubric score or a Luna-quota claim. MERGE HOLD remains.

## Identity and deployment

- Cluster/RG: `daimon-aks` / `daimon-selfhost`; namespace `daimon`; release `coke`.
- Tenant: `91495665-ea07-561f-b769-d05d875385e2`.
- Account: `6bab768b-546b-4196-a870-2bceae3a994c`; CLI caller `operator`.
- Agent: `sh_agent_01M5F52XHR1ZK2N0J9KZ7SMKFW`.
- Environment: `sh_env_01M5F52XJJC8ARWGN9RS2FBMA4`.
- Thread/channel: `aks-proof` / `selfhosted-operator`.
- Session: `sh_session_57d9b493-8a0e-53cc-a4d4-f03cfc23f994`.
- Host used for paid proof: `daimonselfhostacr.azurecr.io/daimon:l15-first-turn-4`, image index
  `sha256:88f53b3f8af7b88838df438b873fc4a78c265b96520f9ccbea972be3b0317e6c`.
- Harness: `daimonselfhostacr.azurecr.io/daimon-harness:l15-first-turn-2`, index
  `sha256:55a31f3f27fff38cb43da5f879949a90e7cf81a91a5570e2028782e5dba2a524`.
- Original host/harness base images: `dev-202610211123`. Overlay Dockerfiles
  are saved here; no Azure resource or AOAI deployment was created/deleted.
- All four service pods Ready at capture: [pods.txt](pods.txt).

## Operator path

`daimon smoke` hardcodes an Anthropic model and creates a new session. `daimon
run` uses billing exemption and bypasses channel admission. The added command
runs **the existing production `admit → bind_session → run_prepared_turn`**
sequence with its private usage recorder. It never supplies a billing exemption,
a fake recorder, a preset Admission, or an injected runtime. The runtime comes
from the exact operator manifest and production runtime factory. Admission
logs showed balance, user-cap, channel-budget and policy checks before the turn.
The funded tenant received a $2 trial ledger credit through `provision_tenant`.

## Exact setup commands

The original scratch directory was obtained with
`mktemp -d /tmp/l15-first-turn.XXXXXX` and was
`/tmp/l15-first-turn.4QeXQP`. Scripts, values and manifest here are the saved
inputs (Python scripts were formatted afterward without behavioral changes). Existing Secret values were never printed; no credential is in this
bundle. Commands below use `E` for this evidence directory and `RUN` for the
original scratch directory.

```bash
export KUBECONFIG=~/.kube/daimon-aks
E="$PWD/docs/evidence/aks-first-turn-20261021"
RUN=/tmp/l15-first-turn.4QeXQP
kubectl patch configmap coredns-custom -n kube-system --type merge \
  -p '{"data":{"l15.override":"log\n"}}'
kubectl rollout restart deployment coredns -n kube-system
kubectl exec -i -n daimon deploy/coke-daimon-mcp -- python < "$E/provision.py"
# Outputs tenant/account/config digest; these appear in runtime.json.
~/.local/bin/az acr login -n daimonselfhostacr
# The final host image additionally shares the unchanged NDJSON reporter outside
# the independent CLI command namespaces; no additional paid turn was made:
docker build -f "$E/Dockerfile.host" \
  -t daimonselfhostacr.azurecr.io/daimon:l15-first-turn-5 .
docker push daimonselfhostacr.azurecr.io/daimon:l15-first-turn-5
docker build -f "$E/Dockerfile.harness" \
  -t daimonselfhostacr.azurecr.io/daimon-harness:l15-first-turn-2 .
docker push daimonselfhostacr.azurecr.io/daimon-harness:l15-first-turn-2
```

The existing chart snapshot lacked a host runtime-file mount. Only the snapshot
at `/tmp/l14-chart.yL0qlx/daimon-selfhost` was extended, using the saved
[host-runtime-secret.patch](host-runtime-secret.patch). Lane 14's source was
not modified. Apply this mount support upstream before treating these commands
as part of the clean-install recipe. `host.runtimeSecret` must not be combined
with that snapshot's Key Vault CSI option; this minimal installation disables it.
Host memory was raised from 512 MiB to 1536 MiB after the init CLI, executed
alongside MCP in the same container, exited 137. No node/resource creation was
needed. The existing database was already migrated; init Jobs were disabled
for these config-only upgrades.

```bash
# Initial empty manifest lets host settings load before resource IDs are known:
printf '[]' > "$RUN/runtime.json"
kubectl create secret generic l15-runtime -n daimon \
  --from-file=runtime.json="$RUN/runtime.json"
# After adding mount support, apply override.yaml with an empty runtime Secret.
helm upgrade coke /tmp/l14-chart.yL0qlx/daimon-selfhost -n daimon \
  --reuse-values -f "$E/override.yaml"
kubectl rollout status deploy/coke-daimon-mcp -n daimon --timeout=120s
kubectl exec -n daimon deploy/coke-daimon-mcp -- daimon selfhosted init \
  --tenant-id 91495665-ea07-561f-b769-d05d875385e2 \
  --harness-url http://coke-daimon-harness:8088 --allow-insecure-http \
  --auth-ref DAIMON_HARNESS_TOKEN --model gpt-5.4-mini
# Output:
# sh_agent_01M5F52XHR1ZK2N0J9KZ7SMKFW
# sh_env_01M5F52XJJC8ARWGN9RS2FBMA4
# Final manifest, populated with those IDs and the admitted configuration digest:
kubectl create secret generic l15-runtime -n daimon \
  --from-file=runtime.json="$E/runtime.json" --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deploy/coke-daimon-mcp deploy/coke-daimon-scheduler -n daimon
kubectl rollout status deploy/coke-daimon-mcp -n daimon --timeout=120s
```

[proof-values.yaml](proof-values.yaml) records the values used for both paid turns;
[override.yaml](override.yaml) records the final host image after the reporter
refactor. Both record mux, channel-backend and selfhosted
admission flags, exact harness auth binding, MCP URL/HTTP grants, model
selection, runtime manifest path and tariff. The Secret `daimon-host` continues
to supply database/JWT/crypto/harness credentials; no values were copied into
Helm. HTTP bearer transmission is explicitly allowed only inside the operator's
trusted cluster network at each host/driver/harness boundary.

Three earlier attempts stopped before paid work: missing runtime mount at
startup; private HTTP rejected by DirectTransport despite stored consent; and
credential-bearing MCP HTTP rejected by the mux layer. The fixes propagate
explicit grants, preserving refusal by default. The successful window made
**four model requests total**, with no extra paid probe.

## Exact turn commands and output

```bash
kubectl exec -n daimon deploy/coke-daimon-mcp -- daimon selfhosted turn \
  --tenant-id 91495665-ea07-561f-b769-d05d875385e2 --thread aks-proof \
  --message 'Use the bash tool to run: printf "AKS_L15_PERSIST_20261021\n" > aks-l15-proof.txt; cat aks-l15-proof.txt. Reply only with the command output.'
kubectl exec -n daimon deploy/coke-daimon-mcp -- daimon selfhosted turn \
  --tenant-id 91495665-ea07-561f-b769-d05d875385e2 --thread aks-proof \
  --message 'Use the bash tool to run: cat aks-l15-proof.txt. Do not write or modify any files. Reply only with the command output.'
```

Both commands exited zero. Both replies were `AKS_L15_PERSIST_20261021`.
[turn1.ndjson](turn1.ndjson) shows bash writing and reading the file, exit 0;
[turn2.ndjson](turn2.ndjson) shows only `cat aks-l15-proof.txt`, exit 0.
The session ID is identical. The on-disk readback is saved in
[file-readback.json](file-readback.json): sandbox UID 10001, content ending in
newline, SHA256 `aaa7dec74073a2152cafc0dbf072e9c19ab7347b93d0d0ed731b4e1f0302a69f`.

## Usage and ledger

Raw provider response `usage` was captured at `_fetch_response`, before the
Agents SDK converts it into `ModelResponse`. The safe provider receipt includes
response ID and model, without prompt/output/credentials. Compare
[harness-turn-window.log](harness-turn-window.log), [c3-events.json](c3-events.json),
and [postgres-evidence.json](postgres-evidence.json).

| C3 request ID | AOAI inclusive input | Cached subset | Output | Ledger debit USD |
|---|---:|---:|---:|---:|
| `16b407b6-007d-42bb-9d98-333ccc7b8659` | 2042 | 0 | 45 | 0.001734 |
| `44e77b18-495c-4f89-a722-e5c0cc96afb9` | 2116 | 1792 | 14 | 0.000440 |
| `7f9ba59c-b5fc-4656-ac38-ffd9641113a5` | 2165 | 1792 | 23 | 0.000518 |
| `52c9440e-1ec8-4d4b-a235-ee1aae5747bf` | 2217 | 1792 | 14 | 0.000516 |

Provider totals: **8540 input, 5376 cached input, 96 output**, zero reasoning.
Each request has one `model_request_started` and exactly one measured C3 `usage`;
each turn ends with `turn_ended`. Four request IDs match four `usage_events`
`event_id` suffixes and four ledger idempotency keys. The legacy `usage_events`
input column is **uncached** input: 2042, 324, 373, 425. Add its separate
`cache_read_input_tokens` column to recover AOAI inclusive input. The modern
usage observations retain native inclusive counts. There are 8 observation
revisions/outbox rows: an initial pending revision and a measured revision for
each request. The four measured revision-2 rows have `applied_at`; the four
unmeasured revision-1 rows retain null `applied_at` and unknown counters.
The verifier checks each measured row and debit; no claim is made that the
unmeasured historical rows were cleaned up. The CLI terminal state's
legacy `usage_totals` is zero; it is not the accounting evidence for this path.

The accounting model is deliberately named **`gpt-5.4-mini-TEST`**. The Azure
[published pricing page](https://azure.microsoft.com/en-us/pricing/details/azure-openai/)
was looked up, but numeric rates could not be verified (dynamic placeholders;
subsequent fetch timed out). These are **TEST rates**, not claimed official
Azure prices: $0.75 uncached input, $0.075 cached input, $4.50 output per million
tokens, effective 2026-10-21, markup 1, infrastructure explicitly zero and funded
separately. The four six-decimal ledger debits total **$0.003208**. Initial credit
was $2; remaining ledger balance is $1.996792.

```bash
kubectl exec -i -n daimon deploy/coke-daimon-mcp -- python < "$E/export-db.py"
kubectl exec -n daimon coke-daimon-harness-0 -- python -c \
 'import sqlite3,json; db=sqlite3.connect("/home/harness/.daimon/sessions/sh_session_57d9b493-8a0e-53cc-a4d4-f03cfc23f994/journal.sqlite3"); print(json.dumps([json.loads(r[0]) for r in db.execute("SELECT body FROM events ORDER BY seq")]))'
python3 "$E/verify.py"
```

`verify.py` checks the stored evidence without model calls: four unique provider
usage tuples, C3 request ordering, exact ledger linkage, token reconciliation,
same-session tool success and persistence, and the egress gap. It prints PASS.

## Initial egress evidence and gap

This section records the initial gap; the lockdown follow-up below closes it.

CoreDNS logging was enabled before any attempt. The export covers
**2026-10-21 11:59:35 UTC through export at 12:00:30 UTC**, enclosing both real
turns. There are 45 log lines, including 20 AOAI resolution lines and **zero
`*.anthropic.com` or `*.openai.com` query matches**. The blocked-curl probes ran
**after** this export, so their intentional Anthropic DNS requests do not
contaminate the turn window.

```bash
kubectl logs -n kube-system -l k8s-app=kube-dns \
  --since-time=2026-10-21T11:59:35Z --tail=-1 --timestamps --prefix
kubectl logs -n daimon coke-daimon-harness-0 \
  --since-time=2026-10-21T11:59:35Z --timestamps
kubectl get netpol coke-daimon-harness -n daimon -o yaml
```

The harness policy permits DNS, the in-cluster MCP port and only
`20.62.58.5/32:443` for AOAI; no arbitrary internet, Anthropic, OpenAI.com or
Blob allowance exists. The saved [harness-policy.yaml](harness-policy.yaml)
is the live object. Postgres is reached by the host, not directly by the harness.

The images lack curl. A copy of this machine's real `/usr/bin/curl`, its ELF
loader and dynamically linked libraries was tarred and transferred with
`kubectl exec -i ... tar xf - -C "$(mktemp -d)"`; no package download or proxy
was used. [export-curl.sh](export-curl.sh), [curl-probe.py](curl-probe.py) and
[curl-results.json](curl-results.json) retain the exact executed commands,
including each pod's scratch directory. The inner command in both pods was:

```bash
SCRATCH/curl/ld-linux-x86-64.so.2 --library-path SCRATCH/curl/lib \
  SCRATCH/curl/curl --noproxy '*' --connect-timeout 5 --max-time 8 \
  -sS -o /dev/null -w 'HTTP=%{http_code}\n' https://api.anthropic.com
```

| Pod | Exit | HTTP | Observation |
|---|---:|---:|---|
| `coke-daimon-harness-0` | 28 | 000 | TCP connection timed out |
| MCP host deployment | 0 | 404 | TLS/HTTP reached Anthropic |

**The v5.1 blocked-curl condition fails for the host.** The current release has
no host NetworkPolicy. The proposed, unapplied
[proposed-host-policy.yaml](proposed-host-policy.yaml) restricts MCP/scheduler
hosts to DNS and the MCP, harness and Postgres pods. It has no external egress.
With Teams disabled, these two hosts should need no external provider access;
validate their health and repeat the two curl checks after chart integration.
Adding Teams later needs narrowly reviewed platform endpoint allowances.
CoreDNS logs alone do not prove the absence of pre-cached-IP connections;
they are the rubric-mandated query evidence, accompanied by the actual harness
policy and negative probe. No claim of full host isolation is made.

## Validation and handoff

- 109 core tests passed (HTTP grants, tools, selfhosted admission/deployment).
- 23 CLI tests passed (new production-path wrapper and existing init suite).
- 86 harness tests passed; two live-test cases deselected, no model calls.
- 76 mux tests passed (private transport refusal and existing wire suite).
- Shared reporter regression: 17 tests passed (including 3 already counted CLI tests).
- Full repository pyright and all 14 import contracts passed after installing
  optional workspace packages; Ruff checks passed.
- Local tests used a dedicated `daimon_test_nc_l15_20261021` database on the
  existing local test Postgres. Tests initially hit local dependency drift;
  reruns pinned relevant packages to `uv.lock`, including Anthropic 0.117.0,
  SQLAlchemy 2.0.49, MCP 1.27.0, OpenAI 2.54.0, Typer 0.24.1 and Click 8.3.2.
- The final host image is `l15-first-turn-5`; its reporting-only refactor preserves
  the captured NDJSON schema and production turn path. That refactor required no
  paid retest; the later authorized lockdown retest used one request. The harness
  image is unchanged from the proof.
- The live deployment retains the runtime Secret, explicit flags, TEST tariff,
  four Ready pods, harness PVC file and CoreDNS logging for evaluator inspection.
- Lane 14 must integrate both saved chart patches (runtime mount and host
  NetworkPolicy). They are already active in the live chart snapshot. Official
  Azure pricing confirmation and Luna quota remain external follow-ups.
- An independent evaluator must rerun/inspect evidence; this lane awards no
  rubric points. No merge was performed; the PR base is `azure-selfhost`.


## Lockdown follow-up — 12:19 UTC

The lead authorized applying the proposed host policy and one short paid
regression turn. The policy now selects every host component of release `coke`
(init, MCP, scheduler and the disabled chat adapters). It permits **only cluster
DNS and same-release Postgres:5432, MCP:8765 and harness:8088 pods**. Other egress
is denied. The host has no direct AOAI allowance; model traffic goes through
the harness policy's AOAI /32. All four service pods remain Ready.

The live object is [lockdown/host-policy.yaml](lockdown/host-policy.yaml).
[host-networkpolicy.patch](host-networkpolicy.patch) adds the Helm template to
lane 14's chart. It is already installed in the live snapshot and owned by Helm;
lane 14 source is untouched. Chart `networkPolicy.enabled` controls both policies.
External integrations such as Teams need separately reviewed endpoint grants;
none is required by the currently enabled MCP/scheduler hosts.

Exact commands, after the original proof:

```bash
kubectl apply -f "$E/proposed-host-policy.yaml"
python3 "$RUN/curl-probe.py"
# Both results: exit 28, HTTP=000, TCP connection timeout.
# Add templates/host-networkpolicy.yaml from host-networkpolicy.patch to snapshot.
# Adopt the policy just created by kubectl into this Helm release:
kubectl label netpol coke-daimon-host-egress -n daimon app.kubernetes.io/managed-by=Helm
kubectl annotate netpol coke-daimon-host-egress -n daimon \
  meta.helm.sh/release-name=coke meta.helm.sh/release-namespace=daimon
helm upgrade coke /tmp/l14-chart.yL0qlx/daimon-selfhost -n daimon \
  --reuse-values -f "$E/override.yaml"
# Fresh window begins 2026-10-21T12:18:59Z, after both intentional curl probes:
kubectl exec -n daimon deploy/coke-daimon-mcp -- daimon selfhosted turn \
  --tenant-id 91495665-ea07-561f-b769-d05d875385e2 --thread aks-proof \
  --message 'Reply only: LOCKDOWN_OK.'
kubectl logs -n kube-system -l k8s-app=kube-dns \
  --since-time=2026-10-21T12:18:59Z --tail=-1 --timestamps --prefix
kubectl logs -n daimon coke-daimon-harness-0 \
  --since-time=2026-10-21T12:18:59Z --timestamps
python3 "$E/lockdown/verify.py"
```

The first Helm adoption attempt correctly refused the kubectl-created policy's
missing ownership metadata; the explicit label/annotations above resolved that.
No egress rule was removed while adopting it. The Helm upgrade succeeded.

[Both curl receipts](lockdown/curl-results.json) now show exit **28**, HTTP **000**.
The one short turn ran at **12:19:04–12:19:06 UTC**, returned **`LOCKDOWN_OK`**,
and retained the original session. [Fresh CoreDNS logs](lockdown/coredns.log)
cover 12:18:59–12:19:48 UTC: **44 lines, zero forbidden-domain queries**.

[Provider receipt](lockdown/harness.log), [C3 events](lockdown/c3-events.json)
and [Postgres rows](lockdown/postgres.json) match for request
`7d729478-632a-490d-89e2-a59d9a066658`: **2244 input, 0 cached, 7 output**;
AOAI response `resp_00a13c2755052f6f016ad8adb900f08196a5d421ba4ae4a79e`.
One measured request produced one usage row and one **$0.001714 TEST debit**.
Program total is now **five** paid model requests, with no additional probes.
The [offline verifier](lockdown/verify.py) checks both blocked curls, this single
request, reply, same session and absence of forbidden DNS. It prints PASS.
The historical verifier remains a check of the initial pre-lockdown snapshot.

CI follow-up fixed the missing strict-docs navigation entry and retained the
legacy transport rejection wording without changing accepted endpoints.
The 99 transport compatibility tests and local strict documentation build pass.
MkDocs' existing tagged YAML is syntax-checked without constructing Python
objects; all other YAML retains the standard safe-load hook.

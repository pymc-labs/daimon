# Daimon on AKS

This chart deploys the Compose host processes (init, MCP, Teams, scheduler,
optional Discord/Slack), one persistent multi-session harness and bundled
PostgreSQL 18. Set `postgres.bundled=false` and `postgres.host` to the Flexible Server hostname
for an external PostgreSQL database.
All Services are ClusterIP. One optional Ingress routes separate MCP and Teams
hostnames, including Teams `/api/messages`; it never publishes the harness or PG.

The chart is a self-hosting baseline on `azure-selfhost`, under the merge hold.
It creates no Azure resources. **Workload Identity token brokering is a stub**:
production identity, RBAC, TLS and network enforcement require operator validation.

## Identity and isolation

Prefer AKS Workload Identity. The harness deliberately rejects plain IMDS,
projected federation tokens and credential files readable by tools. Do not mount
a service account token into that container or change `identity.py` to use
`DefaultAzureCredential`. `automountServiceAccountToken=false` is explicit.

`harness.miProxy.enabled=true` renders an optional sidecar contract, not a proxy
implementation. It fails rendering unless `image`, `clientId` and `tenantId`
are supplied. Implement/review this proxy before using this path:

- Bind only `127.0.0.1:8089`; serve App Service MI `GET /MSI/token` with
  `api-version=2019-08-01`, `resource`, optional `client_id` and the secret
  `X-IDENTITY-HEADER`. Return `access_token`, `expires_on`, `resource`, `token_type`.
- Read `IDENTITY_HEADER` from the referenced Secret (generate at least 32 random
  bytes), compare in constant time, refuse missing/wrong headers and redirects,
  and never log requests, headers or tokens. No token response without the header.
- Use **WorkloadIdentityCredential** and the webhook-projected federation token
  only in the sidecar. Permit only Cognitive Services and Storage resources;
  convert them to `.default` scopes. Refuse other identities/scopes.
- Support the chart's `LISTEN_ADDRESS`, `ALLOWED_RESOURCES`, `IDENTITY_HEADER`
  environment contract. The harness captures its endpoint/header in memory and
  scrubs them before executing tools. Never put the header in arguments or files.

The pod gets the Workload Identity label and the service account gets the client
and tenant annotations. `azure.workload.identity/skip-containers: harness`
excludes the harness from token projection. Do not enable shared process namespace.
Test webhook mutation and prove a tool UID cannot read the proxy token or obtain
a token over loopback. Token exchange also needs Entra HTTPS, allowed only through
explicit `networkPolicy.entraCidrs` when the sidecar is enabled. See Microsoft's
[Workload Identity annotations](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview).

For the M2 pitch on one small node, explicitly reference an existing key Secret:

```yaml
azureOpenAI:
  endpoint: https://YOUR-RESOURCE.openai.azure.com/
  deployment: gpt-6-luna
  apiKeySecretRef:
    name: daimon-aoai
    key: AZURE_OPENAI_API_KEY
```

Create `daimon-aoai` separately from a protected env file. The chart injects only
`AZURE_OPENAI_API_KEY` from that Secret; the harness captures and scrubs it.
`apiKeySecretRef.name` is empty by default. This explicit M2 option can run before
the MI proxy exists; Workload Identity with the header-protected proxy remains
the production identity path. There is no key in chart defaults. With neither key nor
proxy the production harness refuses startup. The offline fixture injects a fake
adapter separately and makes no model calls. Sol (`gpt-6.1-sol`) is the production
parameter; every smoke turn uses Luna (`gpt-6-luna`). `HARNESS_LIVE_PROBE` is never
set by this chart.

The harness entrypoint prepares the HOME PVC as root-owned 0711 before validation,
then remounts `/proc` with `hidepid=2,gid=60002`, then changes
shared scratch directories to 0755, exactly as the CI image probe does. It needs
`SYS_ADMIN` during entry and `DAC_OVERRIDE` for the root startup scan of
otherwise unreadable system directories (matching the CI Docker capability set).
`FOWNER` permits chmod after chown during HOME/journal preparation. All three are dropped by the existing service startup. The existing harness startup changes to UID 10000 and
retains only CHOWN/KILL/SETUID/SETGID; **SYS_ADMIN is absent from the running service
and tools**. The entrypoint also restricts Kubernetes `/dev/termination-log` to 0600 so it
cannot become a tool-readable mounted file. The pod requests only those seven
capabilities, rather than privileged
container mode. Unconfined seccomp and AppArmor permit the mount; this is incompatible
with Baseline/Restricted PodSecurity and usual AKS Azure Policy restrictions.
Use a dedicated namespace/node pool with an explicitly reviewed admission exception.
The chart never labels a production namespace privileged automatically. Do not
set `allowPrivilegeEscalation=false`: UID helpers require the service capabilities.
Do not relax isolation checks to make a restricted pod start.

Multi mode shares one kernel across sessions and is for a trusted tenant. Keep
one replica and one worker for the PVC's SQLite lease. Never share the HOME PVC
between replicas. UID ranges, private scratch, tool limits and checkpoint options
are documented in [the harness README](../../../apps/harness/README.md).
Default requests for PG + MCP/Teams/scheduler + harness total **650m CPU and
1920 MiB RAM**; memory limits total **6 GiB** (harness 4 GiB, three host processes
512 MiB each, PG 512 MiB). This fits a 2-vCPU/8-GiB workload budget; node/system
reservations and the ingress controller still consume capacity. Keep optional
Discord/Slack off, avoid simultaneous build pods, and test actual PyMC workload
memory before increasing concurrency. This is a small pilot, with no application HA.
AKS system node pools do not support B-series VMs; the requested B2ms-class
**resource budget is not a supported AKS SKU promise**. The lead must select an
accepted system-pool VM size/count. Microsoft's system-pool and quota pages give
different minimum sizing details; validate the target AKS API/subscription before
provisioning. See [AKS SKU constraints](https://learn.microsoft.com/en-us/azure/aks/quotas-skus-regions)
and [system pools](https://learn.microsoft.com/en-us/azure/aks/use-system-pools).

The PVC survives pod replacement; configure backups and retention. Optional Blob
checkpoint variables go in `harness.env` and use the header-protected identity;
local PVC durability alone does not protect against disk loss. Bundled PG is a
single instance with no automatic backups, HA or PITR. Prefer Flexible Server for
production; configure and test backups/restore separately. Helm uninstall retains
StatefulSet PVCs; delete them only after a deliberate data-retention decision.

## Networking and MCP TLS

NetworkPolicy defaults to deny harness egress except cluster DNS and the MCP
pods/service port. AOAI/Blob destinations must be explicitly supplied as IPv4
`/32` endpoint addresses. IMDS `169.254.169.254/32` and broad ranges fail rendering.
Prefer private endpoints, keep address lists current, and do not add permissive
policies selecting the harness (Kubernetes combines allows from all policies).
Port 443 only is allowed for endpoint addresses. DNS is limited to kube-system
pods labeled `k8s-app=kube-dns`; adapt for NodeLocal DNS before installation.
The policy admits harness calls only from this release's host pods in its namespace.
NetworkPolicy applies to the entire pod, including the proxy. Node-local traffic,
CNI routing and node compromise need separate platform controls. Validate actual
IMDS denial under the AKS policy engine before production admission.

The MCP process currently serves HTTP. The C3 credential rules **require HTTPS
for a credential-bearing MCP URL**, even inside Kubernetes. Set
`HARNESS_DAIMON_MCP_URL` to the approved public HTTPS MCP URL, and provide an
operator-managed TLS reverse proxy for in-cluster harness access. Its DNS/routing
and NetworkPolicy destination must be explicitly configured. The default policy
allows only the plain MCP Service; it does **not** make credential-bearing MCP
calls to that HTTP Service legal. This TLS/routing integration is a production
gap, not grounds to weaken the harness's URL checks. The local C3 test exercises
built-in tools, not authenticated MCP tool calls.

## Secrets

Create these separately in the release namespace, using protected files and
`kubectl create secret generic ... --from-env-file=/secure/file`:

| Secret | Keys |
| --- | --- |
| `daimon-host` | `DAIMON_DATABASE__URL`, `DAIMON_MCP__JWT_SECRET`, `DAIMON_CRYPTO__KEYS`, `DAIMON_MCP__PUBLIC_URL`, enabled gateway credentials and host harness bearer reference |
| `daimon-harness` | `HARNESS_TOKEN`; optionally Azure API key or operator MCP credentials |
| `daimon-postgres` | `POSTGRES_PASSWORD` (bundled PG only) |
| `daimon-mi-proxy` | `IDENTITY_HEADER` (proxy only) |

Use the same database/user/password in the host URL and bundled PG Secret:
`postgresql+asyncpg://daimon:<url-safe-password>@coke-daimon-postgres:5432/daimon`
for release `coke`. For Flexible Server supply its hostname and `?ssl=verify-full`;
`PGSSLROOTCERT` points at the image's system CA bundle. Test connectivity before
migration. Do not commit the URL, passwords or secrets to values, shell history,
PRs or logs. `host.env`, service `env` and `harness.env` are nonsecret settings only.
Put the Teams client/tenant IDs and secret in `daimon-host`; MCP needs the selected
gateways' settings too, as in Compose. No Azure Bot registration is created.

Optional Key Vault CSI: enable `keyVaultCSI.enabled`, reference an existing
`secretProviderClass`, and configure its `secretObjects` to sync the named Secrets.
Install the AKS provider/driver and configure that class's identity/RBAC separately.
The chart mounts it in each consumer so Secret synchronization starts; set every
provider object's **filePermission: "0400"**, owned by root, so tool UIDs cannot
read projected values. Default world-readable CSI files are unsafe and production
harness startup rejects them. This optional provider/identity path has not been
validated on Azure. Rotate Secrets with a controlled rollout; env values are
captured at process startup.

## AKS recipe — empty resource group to a running pilot

Use this section as the operator recipe. The accompanying
[`scripts/aks.sh`](scripts/aks.sh) executes these phases with `set -euo pipefail`;
all Azure commands below are for the operator, and have not been executed by this
lane. Nothing is scored as live-validated merely because a command is documented.

Prerequisites: Azure CLI authenticated to the target subscription; `kubectl`,
Helm 3.19+, Python 3, git; checkout of the reviewed `azure-selfhost` branch; a
dedicated empty RG (an existing budget is fine). The deployer needs RG Contributor
plus permission to create role assignments (`attach-acr`); RG Owner covers both.
Subscription admins must pre-register `Microsoft.ContainerService`,
`Microsoft.ContainerRegistry`, `Microsoft.CognitiveServices`, `Microsoft.Compute`,
`Microsoft.Network` and `Microsoft.Storage`. RG Owner alone cannot register
providers. Storage registration supports AKS managed disks; no Storage account
or Blob container is created. Kubernetes cluster-admin rights are needed for the
PodSecurity exception, CoreDNS ConfigMap and ephemeral-container egress probe.

Obtain regional Dsv6 CPU quota/capacity for `Standard_D2s_v6` and **DataZoneStandard
Luna quota** before running. Sol quota is needed only for later production use.
Use a US/EU Data Zone region such as `eastus2`; do not silently switch to Global
SKU or another model if quota is zero. Data Zone routing is within the selected
US/EU zone, not a promise of processing within one Azure region. The program's
Luna quota is currently a known external gate. Microsoft documents B-series as
unsupported for system pools; the first live install in this subscription required `Standard_D2s_v6`
(B-series was rejected). This recipe uses that exact one-node SKU; other
subscriptions still require their own quota/API validation.

If providers are not registered, a subscription admin runs these **before** the
timed install (do not run with an RG-only identity and expect them to work):

```sh
for provider in Microsoft.ContainerService Microsoft.ContainerRegistry Microsoft.CognitiveServices Microsoft.Compute Microsoft.Network Microsoft.Storage; do
  az provider register --namespace "$provider" --wait
 done
```

### 1. Select names and build the foundation

Names for ACR/AOAI must be globally unique. Run from the repository root in one
shell; retain `AKS_STATE` securely through reinstall. It contains protected local
env files, including keys. Never enable shell tracing. Only resource names, image
references, URLs and resolved endpoint IPs go into generated Helm values.

```sh
export RG=daimon-selfhost LOCATION=eastus2
export ACR=daimoncoke12345 AKS=daimon-coke AOAI=daimon-coke-12345
export TAG=$(git rev-parse HEAD)
export AKS_STATE=$(mktemp -d)
bash deploy/helm/daimon-selfhost/scripts/aks.sh bootstrap
```

`bootstrap` runs the following commands in order (do not repeat them after the
wrapper already succeeded):

```sh
az group create -n "$RG" -l "$LOCATION" -o none
az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled false -o none
az acr build -r "$ACR" -t "daimon:$TAG" . --no-logs -o none
az acr build -r "$ACR" -t "daimon-harness:$TAG" -f apps/harness/Dockerfile . --no-logs -o none
az acr import -n "$ACR" --source docker.io/library/postgres:18-alpine --image postgres:18-alpine --force -o none
az aks create -g "$RG" -n "$AKS" --node-count 1 --node-vm-size Standard_D2s_v6 \
  --network-plugin azure --network-policy azure --attach-acr "$ACR" \
  --enable-managed-identity --generate-ssh-keys --tier free --node-osdisk-size 64 -o none
az cognitiveservices account create -g "$RG" -n "$AOAI" -l "$LOCATION" \
  --kind OpenAI --sku S0 --custom-domain "$AOAI" -o none
az cognitiveservices account deployment create -g "$RG" -n "$AOAI" \
  --deployment-name gpt-6-luna --model-name gpt-6-luna --model-version 2026-09-22 \
  --model-format OpenAI --sku-name DataZoneStandard --sku-capacity 1 -o none
```

**Mandatory Postgres image step:** `images.registry` prefixes **all three**
images, including PostgreSQL and its new readiness init containers. The
`az acr import` above must complete before Helm: without it, AKS attempts
`<acr>.azurecr.io/postgres:18-alpine` and enters ImagePullBackOff. To repair an
existing registry, run that same import command; host/harness builds do not
create the Postgres repository. The helper verifies the imported image exists.

ACR builds the current checkout remotely. It contains no `.env` or credentials;
review `.dockerignore` before building. No registry password is generated;
`attach-acr` grants image pulls to the AKS kubelet identity. No Key Vault, Blob,
Postgres Flexible or ACA is provisioned. AKS itself creates a managed node RG with
the VM, OS/data disks, network, load balancer and public IP resources. These are
billable even though there are only three top-level application services.

### 2. Create Kubernetes Secrets and install

```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh install
```

The helper obtains the AOAI endpoint/key through captured Azure CLI stdout,
generates PG password, bearer, MCP signing and Fernet keys, and writes env files
under the private `AKS_STATE` directory (umask 077). It never prints their values.
It creates `daimon-postgres`, `daimon-host`, `daimon-harness`, `daimon-aoai` Secrets:

```sh
az aks get-credentials -g "$RG" -n "$AKS" --overwrite-existing -o none
kubectl create namespace daimon --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace daimon pod-security.kubernetes.io/enforce=privileged --overwrite
for pair in postgres:pg host:host harness:harness aoai:aoai; do
  kubectl -n daimon create secret generic "daimon-${pair%:*}" \
    --from-env-file="$AKS_STATE/${pair#*:}.env" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done
helm install coke deploy/helm/daimon-selfhost -n daimon -f "$AKS_STATE/aks-values.json" \
  --wait --wait-for-jobs --timeout 15m
kubectl -n daimon rollout status statefulset/coke-daimon-harness --timeout=300s
kubectl -n daimon wait --for=condition=ready pod --field-selector=status.phase!=Succeeded --timeout=300s
```

The Job and every host Deployment first run a bounded `pg_isready` init
container (`postgres.waitTimeoutSeconds`, default 300). Hosts then wait for
`alembic current --check-heads` before starting; the Job runs upgrade/defaults.
The Job allows two retries and has a 900-second total deadline. This ordering
lets **one clean `helm install --wait --wait-for-jobs`** deploy everything without
racing Postgres or starting scheduler against an unmigrated schema. Completed
Job pods are excluded from subsequent pod-Ready waits. Failed migrations remain
visible Job failures; do not enable traffic or hide a real migration error.

The generated nonsecret values use ACR, Luna, `azureOpenAI.apiKeySecretRef`, and
current AOAI IPv4 `/32` addresses, resolved from the actual account hostname.
The first live install resolved **`20.62.58.5/32`**. This is observed evidence,
not a portable default: resolve your own endpoint at install time, as the helper
does with Python `socket.getaddrinfo`, and refresh it when DNS changes. For
example, the nonsecret value from that installation was:

```yaml
networkPolicy:
  azureOpenAICidrs: [20.62.58.5/32]
```

Only AOAI HTTPS, DNS and the in-cluster MCP
Service are allowed from the harness; no Blob destination is supplied. PG is
used by host processes, not by the harness. IP changes require regenerating the
allowlist and a controlled Helm update; use private endpoints for production.
`imagePullSecrets` remains empty. Teams is disabled until real Bot credentials
exist, so this minimal phase has four Ready serving/stateful pods. Admission
stays off until the host tenant/channel/billing grant is configured below. Host
Anthropic URL is deliberately loopback port 9, so startup reconciliation cannot
contact Anthropic even when another provider's defaults are present.

The installer checks MCP `/healthz`. A separate **paid Luna-only** C3 preflight
checks model usage, a write tool and a second turn reading the same file:

```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh probe
```

This directly invokes C3 and is not a CLI/Teams turn or a Postgres metering-ledger
proof. To score a real Daimon turn, configure registration and the host runtime
grant in the following section, then use the authorized CLI/Teams entry point.
Those agent/tenant/channel/billing inputs are deployment-specific. Preserve the
provider request IDs, harness events and ledger evidence; do not substitute the
direct preflight for that application test.

### 3. Egress evidence for the entire turn window

```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh egress-start
# Run the real authorized Daimon CLI/Teams turns, including write then read.
bash deploy/helm/daimon-selfhost/scripts/aks.sh egress-check
```

`egress-start` applies this standard AKS logging override and restarts CoreDNS:

```yaml
apiVersion: v1
kind: ConfigMap
metadata: {name: coredns-custom, namespace: kube-system}
data:
  log.override: |
    log
```

Equivalent commands used by the helper:

```sh
kubectl -n kube-system create configmap coredns-custom --from-literal=log.override=log \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n kube-system rollout restart deployment/coredns
kubectl -n kube-system rollout status deployment/coredns --timeout=120s
```

The helper records UTC start, performs a positive AOAI DNS canary, and captures
all CoreDNS replica logs for the complete window into `AKS_STATE/turn-dns.log`.
It refuses evidence without the canary or with any `*.anthropic.com`/`*.openai.com`
query (Azure's `*.openai.azure.com` is the intended provider). Preserve these logs
and turn timestamps. DNS logs alone cannot detect cached or hard-coded-IP traffic;
combine them with the default-deny policy and the negative connection test.
See Microsoft's [AKS CoreDNS logging](https://learn.microsoft.com/en-us/azure/aks/coredns-troubleshoot)
and [Azure NetworkPolicy setup](https://learn.microsoft.com/en-us/azure/aks/use-network-policies).

After snapshotting the clean turn window, `egress-check` resolves Anthropic via
allowed DNS, then runs **curl in an ephemeral container within the actual harness
pod's network namespace**. The harness image has no curl, so no package install
or capability is added to the service. Azure NetworkPolicy applies to the whole
pod. The command is equivalent to:

```sh
kubectl -n daimon debug pod/coke-daimon-harness-0 --image=curlimages/curl:8.16.0 \
  --container="$probe" --attach=false -- curl --noproxy '*' --connect-timeout 5 --max-time 10 \
  --resolve "api.anthropic.com:443:$denied_ip" -sS -o /dev/null https://api.anthropic.com
```

The helper supplies `probe`/`denied_ip`, waits for exit **28** (connection timeout),
and saves/prints the error. HTTP 401, TLS failure, missing curl and DNS failure do
not count as blocked egress. This explicit negative probe happens after the turn
log snapshot because it intentionally introduces a forbidden-domain lookup.
Do not claim egress proof if any other NetworkPolicy admits broad harness egress.

### TEARDOWN and reinstall (target: under 30 minutes)

For a disposable pilot, `teardown` removes the Helm release, namespace **including
both PVCs and their data**, AKS cluster and managed node RG. Confirm backups or
accept loss of all tenants, ledger, sessions and workspaces before running it.
It keeps **ACR/images and AOAI/Luna deployment**, plus the protected operator state;
ACR/storage and AOAI usage remain billable. It checks the managed node RG was
removed. An incomplete deletion is a failed teardown, not a passing result.

```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh teardown
```

Underlying destructive commands:

```sh
helm uninstall coke -n daimon --ignore-not-found
kubectl delete namespace daimon --wait=true --ignore-not-found
az aks delete -g "$RG" -n "$AKS" --yes -o none
```

Retaining built images and model quota avoids rebuilds and model provisioning on
the timed path. In the same shell, start the clock before re-creating AKS, run the
same install, re-register the application state below, and stop only after the
first successful **Daimon CLI/Teams** Luna reply. Record timestamps and elapsed
seconds. Thirty minutes is a target, **not measured Azure evidence** from this
lane; capacity, image pulls, RBAC propagation and application setup may exceed it.

```sh
date -u +%Y-%m-%dT%H:%M:%SZ > "$AKS_STATE/reinstall-start"
time bash deploy/helm/daimon-selfhost/scripts/aks.sh reinstall
# Re-register tenant/channel/runtime grant; send the first real Daimon turn.
date -u +%Y-%m-%dT%H:%M:%SZ > "$AKS_STATE/reinstall-first-reply"
```

`reinstall` uses exactly the same `az aks create` above and calls `install`, with
no rebuild or AOAI recreation. It does not pretend to restore deleted PG/PVC data.
For complete removal (also deletes the budget), run only on this dedicated RG:

```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh remove
rm -r "$AKS_STATE"
```

`remove` runs `az group delete -n "$RG" --yes -o none`. Verify no retained
node/data-disk resource group remains before saying billing has stopped. Avoid
reusing a deleted AOAI name without handling its Azure soft-delete reservation.
For overnight shutdown use teardown; a system node pool cannot generally scale to
zero, and stopping a cluster retains billable disks/IPs.

Planning allowance: roughly **$100–170/month** for one continuously running D2s_v6
pilot, disks, networking and ACR, **plus model tokens/builds/egress**; this is an
unverified planning range, not a price quote. Recalculate for the actual region,
SKU/contract and 730 hours; enable the existing $25/$50 budget alerts. Free AKS
control-plane tier has no SLA and does not make node resources free. Sources:
[AKS pricing](https://azure.microsoft.com/en-us/pricing/details/kubernetes-service/),
[VM pricing](https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/),
[ACR pricing](https://azure.microsoft.com/en-us/pricing/details/container-registry/).

## Register the selfhosted environment

For release `coke`, execute from a host pod (or a one-off host-image Job with the
host Secret). Tenant must already exist. This makes no Anthropic/model calls:

```sh
daimon selfhosted init --tenant-id '<tenant-uuid>' \
  --harness-url http://coke-daimon-harness:8088 --allow-insecure-http \
  --auth-mode bearer_env --auth-ref DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY \
  --model gpt-6-luna
```

Equivalent environment `native_config.value` (the CLI defaults session_pool false):

```json
{
  "harness_url": "http://coke-daimon-harness:8088",
  "allow_insecure_http": true,
  "auth_mode": "bearer_env",
  "auth_ref": "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY",
  "transport": "direct",
  "session_pool": false
}
```

Set that host Secret variable to the harness's bearer. Bind it through
`DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS` to this exact HTTP origin and tenant.
Configure the channel's selfhosted revision/runtime grant and verified billing
rates as described in [self-hosting](../../../docs/self-hosting.md). Admission
remains disabled by default (C8). Enable
`host.env.DAIMON_SELFHOSTED__ADMISSION_ENABLED=true` only after these gates and live
usage/reconciliation checks pass. Production agent registration uses Sol; tests
always explicitly select Luna. Never put credentials in the registration JSON.

## Local validation

Linux amd64, Docker, uv, Python 3 and curl are required. Binaries are pinned and
SHA256-verified against the upstream release checksum files, installed in
`~/.local/bin`. No Azure credentials or resources are used:

```sh
export PATH="$HOME/.local/bin:$PATH"
bash deploy/helm/daimon-selfhost/tests/install-tools.sh
docker build -t daimon-l14:local .
docker build -f apps/harness/Dockerfile -t daimon-harness-l14:local .
bash deploy/helm/daimon-selfhost/tests/kind-smoke.sh
# The script always deletes its exact kind cluster, including on failure.
# After investigation, prune only these tags if desired:
docker image rm daimon-l14:local daimon-harness-l14:local
```

The script loads images and post-renders only offline fixtures: a 20-second
Postgres startup delay and the fake harness adapter. It installs PG, the init
Job and all host processes together with `--wait --wait-for-jobs`, asserts revision
one is deployed with no main-container restarts, and verifies all serving pods
Ready, MCP health, authenticated C3 health, unauthorized rejection, two sessions,
tool writes, fake usage, event polling, idempotency and replay after pod replacement. It verifies the multi-mode
proc/scratch checks, service UID 10000, SYS_ADMIN removal and unprivileged tool UID.
The fixture uses `HARNESS_TEST_MODE=1` because this lab's container runtime injects
readable synthetic DMI identity files into kind pods; that is a deliberate
credential-file-scan bypass in an offline fake fixture, never chart configuration.
It explicitly applies the production privilege-drop/protection helpers. Production
AKS must pass all startup checks without this mode. It runs kubeconform when
available and API server dry-run schema validation. kindnet, kind's default CNI,
does **not enforce NetworkPolicy**; the script reports that gap explicitly.
For enforcement tests use a Calico/Cilium kind cluster or AKS: verify DNS and MCP
are reachable, approved HTTPS destinations work, other destinations and IMDS
are denied, and a tool UID cannot use loopback proxy or read federation files.
Offline success does not prove Azure identity, model access or Teams Bot delivery.

## Next: one agent pod per session (F72)

Carlos's target AKS diagram has one agent pod per session. V1 deliberately uses
`harness.sessions.mode: multi` and one StatefulSet replica. Other mode values fail
rendering. The next step is a **session router** managing one single-mode harness
pod/PVC per session, stable C3 routing across turns/actions/cancel/polls, lifecycle,
resource limits and recovery. `session_pool` here stays false: the current true
value means the ACA pool protocol, not a Kubernetes router. Optional AKS Pod
Sandboxing/Kata can strengthen the per-session kernel boundary, subject to AKS
node/runtime support. No router, single-mode deployment or Kata guarantee ships
in this chart. Keep the mode under `harness.sessions` when that work is added.

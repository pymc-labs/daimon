# Daimon on AKS

This chart deploys the Compose host processes (init, MCP, scheduler,
optional Teams/Discord/Slack), one persistent multi-session harness and bundled
PostgreSQL 18. Set `postgres.bundled=false` and `postgres.host` to the Flexible Server hostname
for an external PostgreSQL database.
All Services are ClusterIP. Optional Ingress resources route separate MCP and Teams
hostnames, including Teams `/api/messages`, `/api/graph/notifications` and `/api/graph/lifecycle`; it never publishes the harness or PG.

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
  deployment: gpt-5.4-mini
  apiKeySecretRef:
    name: daimon-aoai
    key: AZURE_OPENAI_API_KEY
```

Create `daimon-aoai` separately from a protected env file. Tenants enforcing Azure OpenAI **disable local auth** cannot use the key path:
`keys list`/key inference fails; a reviewed MI proxy is required before install.
The chart injects only
`AZURE_OPENAI_API_KEY` from that Secret; the harness captures and scrubs it.
`apiKeySecretRef.name` is empty by default. This explicit M2 option can run before
the MI proxy exists; Workload Identity with the header-protected proxy remains
the production identity path. There is no key in chart defaults. With neither key nor
proxy the production harness refuses startup. The offline fixture injects a fake
adapter separately and makes no model calls. The AKS recipe and smoke turns use
`gpt-5.4-mini`. `HARNESS_LIVE_PROBE` is never
set by this chart.

The harness entrypoint prepares the HOME PVC as root-owned 0711 before validation,
then remounts `/proc` with `hidepid=2,gid=60002`, then changes
shared scratch directories to 0755, exactly as the CI image probe does. It needs
`SYS_ADMIN` during entry and `DAC_OVERRIDE` for the root startup scan of
otherwise unreadable system directories (matching the CI Docker capability set).
`FOWNER` permits chmod after chown during HOME/journal preparation. After the
remount, `setpriv --bounding-set=-sys_admin,-setpcap` permanently removes SYS_ADMIN from
the service's bounding set, including later root helper execs. Harness startup
changes to UID 10000 and retains only CHOWN/KILL/SETUID/SETGID; tools run with no
capabilities. Root helpers can regain DAC_OVERRIDE and FOWNER from the bounding
set after returning to UID 0; these support startup scanning and ownership changes.
A compromised service retaining SETUID can therefore bypass file permissions;
this is a trusted-service boundary, not protection against service compromise.
Kubernetes `/dev/termination-log` is restricted to 0600.
SETPCAP is granted only to perform this bounding-set drop and removes itself too.
The pod requests eight capabilities for startup rather than privileged mode.
Seccomp is RuntimeDefault; AppArmor is Unconfined for the remount. This remains
incompatible with Baseline/Restricted PodSecurity and usual AKS Azure Policy.
Use a dedicated namespace/node pool with an explicitly reviewed admission exception.
The chart never labels a production namespace privileged automatically. Do not
set `allowPrivilegeEscalation=false`: UID helpers require the service capabilities.
Do not relax isolation checks to make a restricted pod start.

Multi mode shares one kernel across sessions and is for a trusted tenant.
User namespaces remain kernel attack surface under RuntimeDefault; this does not
provide a per-session VM security boundary. Keep
one replica and one worker for the PVC's SQLite lease. Never share the HOME PVC
between replicas. UID ranges, private scratch, tool limits and checkpoint options
are documented in [the harness README](../../../apps/harness/README.md).
Default steady-state requests for PG + MCP/scheduler + harness total
**250m CPU / 1408 MiB RAM**. Enabling Teams brings this to **300m / 1664 MiB**.
The init Job adds 50m / 256 MiB temporarily. Host migration init containers request
50m / 128 MiB; Kubernetes schedules each pod against its larger effective request.
Memory limits remain 9 GiB with Teams, 7.5 GiB without (harness 4 GiB, each host
1536 MiB, PG 512 MiB), allowing CLI work alongside MCP but overcommitting a small
node. Concurrency and PyMC workloads must be measured and bounded.

**Standard_D2s_v6 is the minimum test size; Standard_D4s_v6 is the production
recommendation.** The observed D2s_v6 node had **1900m CPU / 5.66 GiB allocatable**,
less than its advertised 2 vCPU / 8 GiB. System pods and any ingress controller
consume that allocatable budget. Release-and-namespace-scoped PriorityClasses rank Postgres
(20000), then harness (10000), above ordinary host pods (0); system-critical pods
can still preempt them. These priorities reduce application contention, not
hardware or memory-pressure risk. Keep optional gateways off on D2s_v6 and avoid
simultaneous build pods. This single-node pilot has no application HA. AKS system
pools reject B-series; validate quota and SKU availability in the target subscription.

The PVC survives pod replacement; configure backups and retention. This minimal
multi-session deployment has no remote checkpoints; local PVC durability alone
does not protect against disk loss. Bundled PG is a
single instance with no automatic backups, HA or PITR. Prefer Flexible Server for
production; configure and test backups/restore separately. Helm uninstall retains
StatefulSet PVCs; delete them only after a deliberate data-retention decision.

## Networking and MCP TLS

Host pods (init, MCP, Teams, scheduler, Discord, Slack) also default-deny egress.
They allow only cluster DNS and same-release PostgreSQL, MCP and harness pods,
within the release namespace. Host calls to Anthropic/OpenAI.com are therefore
blocked by an enforcing CNI as well as the pilot's loopback Anthropic setting.
An external PostgreSQL deployment additionally requires explicit
`networkPolicy.hostPostgresCidrs` IPv4 `/32`s, on `postgres.port`; no addresses
are allowed implicitly. The policy covers init containers too. External
Teams/Discord/Slack APIs and third-party MCP destinations require separately
reviewed, narrow policies before enabling those integrations.
Teams defaults off. Enable `services.teams.enabled` and configure its external
dependencies before attempting delivery. Teams has its own
`networkPolicy.teamsEgress.enabled` toggle and `cidrs` list:
resolve the Bot Framework and Entra endpoint addresses to explicit `/32`s and
keep them current. This policy selects only the Teams host pod, preserving the
harness lockdown; never turn off the global policy to enable Teams. Avoid broad
policies: Kubernetes unions egress allowances across matching policies.

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

Both MCP (8765) and harness C3 (8088) Services serve **HTTPS through Uvicorn**.
Helm generates a CA with `genCA` and a leaf with `genSignedCert`; SANs cover
`<release>-daimon-mcp` and `<release>-daimon-harness`, their namespace-qualified
names, `.svc` and `.svc.<mcpTLS.clusterDomain>` (default `cluster.local`). Use
`https://coke-daimon-mcp:8765/mcp` and `https://coke-daimon-harness:8088`.
No bearer travels over plaintext in this recipe.

The generated `<release>-daimon-mcp-tls` Secret contains `ca.crt`, `tls.crt` and
`tls.key`; the CA private key is discarded. Online upgrades reuse it via `lookup`.
Offline `helm template` generates fresh certificates: never pipe that over an
existing release. Leaf validity defaults to 365 days (`mcpTLS.validityDays`), CA
validity to 3650 days. Monitor expiry. For rotation, prepare a replacement Secret,
set `mcpTLS.existingSecret`, upgrade, restart all hosts and the harness and verify
TLS. Keep the old Secret until all workloads roll. Changes to Secret contents
alone do not reload Uvicorn or the copied trust bundles.

Optional operator/cert-manager Secrets must provide the same three keys and all
SANs; cert-manager is not installed by the chart. Both server keys mount read-only
as **0400 root-only**. Uvicorn loads its SSL context before the harness app factory
drops to service UID 10000; MCP loads its context then drops to host UID/GID 1000.
The private key stays unreadable after the drop. MCP drops ALL default
capabilities, adds only SETUID/SETGID for the initial UID change, disables privilege
escalation and uses RuntimeDefault seccomp. Recipe commands run operator processes
as UID/GID 1000 via `setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs`
with `HOME=/home/daimon`; use the same prefix for manual MCP `kubectl exec`. A host init container combines
certifi roots with the chart CA in a read-only volume; the chart sets
`SSL_CERT_FILE=/etc/daimon/ca/ca-bundle.crt` for all trusted host clients.

Harness MCP trust remains scoped: an init container copies only the public CA to
an in-memory volume, UID/GID 10000, mode 0400. It mounts read-only at
`/etc/daimon/mcp-ca/ca.crt`, with
`HARNESS_MCP_CA_BUNDLE=/etc/daimon/mcp-ca/ca.crt` fixed by the chart. Tools cannot
read it. Future sandbox containers must use this mount/env interface. The harness
MCP client from the application prerequisite loads it alongside system roots;
Azure OpenAI trust stays unchanged. Never disable certificate or hostname checks.

MCP and Teams have separate optional Ingress resources. MCP configures
ingress-nginx verified HTTPS upstream using the TLS Secret and MCP Service name;
Teams retains HTTP upstream. Other controllers need equivalent settings in
`ingress.mcpAnnotations`. Public `ingress.tls` Secrets remain operator-managed.
Harness ingress remains limited to same-release host pods in its namespace.
Validate enforcement with the deployed CNI; kindnet does not enforce policies.
DNS queries remain a possible tool exfiltration path (F77); DNS logging is evidence,
not a complete prevention mechanism.

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

Every helper phase that uses Helm or kubectl fetches credentials for exactly
`$RG/$AKS` into a fresh private kubeconfig under `AKS_STATE`, exports it to child
processes, and removes it on exit. The helper never reads or changes
`~/.kube/config` or uses an inherited `KUBECONFIG`. It verifies the current API
server against the target cluster's Azure `fqdn`, `privateFqdn` or
`azurePortalFqdn` before workload operations, including teardown. Credential or
server verification failures stop the phase before Helm/kubectl mutations.
The operator commands in `INSTALL-COKE.md` are unchanged. For manual kubectl or
Helm commands shown below, fetch a separate private kubeconfig explicitly with
`az aks get-credentials -g "$RG" -n "$AKS" --file "$KUBECONFIG"`; set
`KUBECONFIG` to a file in your own `mktemp -d` directory first.

Prerequisites: Azure CLI authenticated to the target subscription; `kubectl`,
Helm 3.19+, Python 3, git; checkout of the reviewed `azure-selfhost` branch; a
dedicated RG containing only its budget and, optionally, the existing AOAI account
and deployment. Azure CLI must support `az acr run`; bootstrap checks this before
creating resources. Images build remotely with the ACR Tasks BuildKit builder;
local Docker/buildx is not required for bootstrap. The deployer needs RG Contributor
plus permission to create role assignments (`attach-acr`); RG Owner covers both.
Subscription admins must pre-register `Microsoft.ContainerService`,
`Microsoft.ContainerRegistry`, `Microsoft.CognitiveServices`, `Microsoft.Compute`,
`Microsoft.Network` and `Microsoft.Storage`. RG Owner alone cannot register
providers. Storage registration supports AKS managed disks; no Storage account
or Blob container is created. Kubernetes cluster-admin rights are needed for the
PodSecurity exception, CoreDNS ConfigMap and ephemeral-container egress probe.

Obtain regional Dsv6 CPU quota/capacity for `Standard_D2s_v6` and an AOAI
deployment of a supported model (we use **gpt-5.4-mini**). An existing account
and deployment are reused without updating either. Set `AOAI` to that account's
name and `AOAI_DEPLOYMENT` to its deployment name. If the account exists but the
named deployment is missing, bootstrap fails clearly; provision it before retrying.
For a new account, regional DataZoneStandard quota for gpt-5.4-mini is required.
Use a US/EU Data Zone region such as `eastus2`; do not silently switch to Global
SKU or another model. Data Zone routing is within the selected US/EU zone,
not a promise of processing within one Azure region. Microsoft documents B-series as
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
export AOAI_DEPLOYMENT=gpt-5.4-mini
export AKS_STATE=$(mktemp -d)
bash deploy/helm/daimon-selfhost/scripts/aks.sh bootstrap
```

When `TAG` is unset, each bootstrap generates a unique git-SHA/timestamp tag.
After a successful build it saves the tag in `AKS_STATE/image-tag`; later helper
commands reuse that saved tag. An explicit operator `TAG` takes precedence.

`bootstrap` builds and pushes both production Dockerfiles using
[`scripts/acr-build.yaml`](scripts/acr-build.yaml), with `DOCKER_BUILDKIT=1` on
each build step. The task also selects `docker/dockerfile:1.7.0@sha256:dbbd5e059e8a07ff7ea6233b213b36aa516b4c53c645f1817a4dd18b83cbea56` via `BUILDKIT_SYNTAX` for
the harness `ADD --checksum`. This enables `COPY --chmod` and `RUN --mount`; plain
`az acr build` uses a legacy builder and a Dockerfile syntax header alone does
not enable BuildKit. [`scripts/build-images.sh`](scripts/build-images.sh) queues
with `--no-wait`, captures the run ID from the CLI queue receipt and polls `az acr task show-run` until
exactly `Succeeded`. Failed/canceled/error/timeout/unknown states, failed status
lookups and a local wait timeout all stop provisioning, even if the tag already
exists. The wait defaults to 7200 seconds with 15-second polls; positive integer
`ACR_BUILD_WAIT_SECONDS` and `ACR_BUILD_POLL_SECONDS` can override those limits.
Polling and final image checks are implemented in
[`scripts/wait-build.sh`](scripts/wait-build.sh). Logs are fetched after a terminal
result for diagnostics; their exit code never
authorizes provisioning. A missing ACR Tasks CLI
fails before resource creation; a remote build/push failure stops before AKS
creation. After confirmed run success, bootstrap also verifies that both requested
image tags exist in ACR; a missing or unreadable image stops before Postgres
import and AKS creation. The remote builder leaves no local image tags or build cache.

`bootstrap` runs the following commands in order (do not repeat them after the
wrapper already succeeded):

```sh
az group create -n "$RG" -l "$LOCATION" -o none
az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled false -o none
# The helper below includes run polling, both image checks and tag persistence.
export TAG=${TAG:-$(cat "$AKS_STATE/image-tag")}
bash deploy/helm/daimon-selfhost/scripts/build-images.sh
python3 deploy/helm/daimon-selfhost/scripts/import-postgres.py
az aks create -g "$RG" -n "$AKS" --node-count 1 --node-vm-size Standard_D2s_v6 \
  --network-plugin azure --network-plugin-mode overlay --network-policy azure --attach-acr "$ACR" \
  --enable-managed-identity --generate-ssh-keys --tier free --node-osdisk-size 64 -o none
accounts=$(az cognitiveservices account list -g "$RG" --query '[].name' -o tsv)
if grep -Fxq -- "$AOAI" <<< "$accounts"; then
  az cognitiveservices account deployment show -g "$RG" -n "$AOAI" \
    --deployment-name "$AOAI_DEPLOYMENT" -o none || {
      echo "Existing AOAI account is missing or cannot read deployment $AOAI_DEPLOYMENT; provision it before bootstrap" >&2
      exit 1
    }
  echo "Reusing existing AOAI account and deployment $AOAI_DEPLOYMENT"
else
  az cognitiveservices account create -g "$RG" -n "$AOAI" -l "$LOCATION" \
    --kind OpenAI --sku S0 --custom-domain "$AOAI" -o none
  az cognitiveservices account deployment create -g "$RG" -n "$AOAI" \
    --deployment-name "$AOAI_DEPLOYMENT" --model-name gpt-5.4-mini --model-version 2026-03-17 \
    --model-format OpenAI --sku-name DataZoneStandard --sku-capacity 1 -o none
fi
```

**Mandatory Postgres image step:** `images.registry` prefixes **all three**
images, including PostgreSQL and its new readiness init containers. The
[`scripts/import-postgres.py`](scripts/import-postgres.py) must complete before
AKS creation: without it, AKS attempts `<acr>.azurecr.io/postgres:18-alpine` and
enters ImagePullBackOff. The importer copies the reviewed index
`sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873`
from `public.ecr.aws/docker/library/postgres` first, then `docker.io/library/postgres`
as fallback, alternating between the two until success or the shared time budget
expires. Both sources use the exact digest, rather than a moving upstream tag.
`POSTGRES_IMPORT_BUDGET_SECONDS` defaults to 900 (15 minutes), including import
commands, verification and sleeps. Each CLI call is also capped at
`POSTGRES_IMPORT_ATTEMPT_SECONDS` (default 180 seconds), never exceeding the
remaining total budget. A stalled attempt is retryable: the helper kills its
local CLI process group, reaps the CLI, and moves to the next source while time
remains. Run these helpers on Linux or WSL for POSIX process-group cleanup.
Retries use exponential backoff starting at
5 seconds, jittered by ±20% and capped at 60 seconds and the remaining budget.
Public mirrors can also rate-limit; the budget accommodates several minutes of
429/5xx failures. Each failed attempt reports its source, attempt number and a
one-line sanitized error excerpt.

Optionally set both `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` in the protected
operator environment. They are passed as `--username`/`--password` only for
Docker Hub imports and are redacted from diagnostics; neither enters Helm values.
Set both or neither. Anonymous imports remain the default.

A tag resolving alone is insufficient: the helper verifies the pinned index,
its Linux/amd64 descriptor and the actual referenced child manifest in ACR.
An incomplete import or an existing tag pointing at another digest is repaired
with `--force` from the pinned source. A digest mismatch after import is refused.
Already-complete pinned images skip import. Budget exhaustion stops before AKS creation;
only verified success writes `AKS_STATE/postgres-digest`. To repair a partial
registry, rerun this same helper with `ACR` and `AKS_STATE` set. Host/harness builds
do not create the Postgres repository. Azure CLI must support `az acr manifest show`.
ACR needs access to both public sources during import; these are build-time
sources, not additional pod egress allowances. No new credentials are needed.

For an isolated import check, set `POSTGRES_IMPORT_REPOSITORY` to a throwaway
repository name when running the helper directly. Bootstrap always imports into
`postgres` so Helm uses the verified image.
`POSTGRES_IMPORT_RETRY_SECONDS` optionally sets the initial retry delay (0–60,
default 5); subsequent delays double up to the cap. The total budget is
configurable from greater than zero up to 86400 seconds.

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
Install retries preserve registration settings and restore the saved runtime Secret
before Helm if one is configured; retain `runtime.json` with `aks-values.json`.
It creates `daimon-postgres`, `daimon-host`, `daimon-harness`, `daimon-aoai` Secrets:

```sh
az aks get-credentials -g "$RG" -n "$AKS" --overwrite-existing -o none
kubectl create namespace daimon --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace daimon pod-security.kubernetes.io/enforce=privileged --overwrite
for pair in postgres:pg host:host harness:harness aoai:aoai; do
  kubectl -n daimon create secret generic "daimon-${pair%:*}" \
    --from-env-file="$AKS_STATE/${pair#*:}.env" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done
helm upgrade --install coke deploy/helm/daimon-selfhost -n daimon -f "$AKS_STATE/aks-values.json" \
  --wait --wait-for-jobs --timeout 15m
kubectl -n daimon rollout status statefulset/coke-daimon-harness --timeout=300s
kubectl -n daimon rollout status deployment --timeout=300s
kubectl -n daimon rollout status statefulset --timeout=300s
```

The Job and every host Deployment first run a bounded `pg_isready` init
container (`postgres.waitTimeoutSeconds`, default 300). Hosts then wait for
`alembic current --check-heads` before starting; the Job runs upgrade/defaults.
The Job allows two retries and has a 900-second total deadline. This ordering
lets **one clean `helm install --wait --wait-for-jobs`** deploy everything without
racing Postgres or starting scheduler against an unmigrated schema. Completed
Job pods are excluded from subsequent pod-Ready waits. Failed migrations remain
visible Job failures; do not enable traffic or hide a real migration error.

The generated nonsecret values use ACR, gpt-5.4-mini, `azureOpenAI.apiKeySecretRef`, and
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

The installer checks MCP `/healthz`. A separate **paid gpt-5.4-mini** C3 preflight
checks model usage, a write tool and a second turn reading the same file. It
requires matching write/read call IDs and the exact per-run persistence marker.
`SELFHOSTED_MODEL` defaults to `AOAI_DEPLOYMENT` (default `gpt-5.4-mini`); the
probe refuses a different deployment or any Sol model. Before invoking the
harness, it queries Azure deployment metadata and requires the underlying model
to be exactly `gpt-5.4-mini`, including for an alias such as `production`.
Bootstrap applies the same check when reusing a deployment. Direct `live-c3.py`
execution requires `AOAI_VERIFIED_MODEL=gpt-5.4-mini` from that metadata check:

```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh probe
```

This directly invokes C3 and is not a CLI/Teams turn or a Postgres metering-ledger
proof. To score a real Daimon turn, configure registration and the host runtime
grant in the following section, then use the authorized CLI/Teams entry point.
Those agent/tenant/channel/billing inputs are deployment-specific. Preserve the
provider request IDs, harness events and ledger evidence; do not substitute the
direct preflight for that application test.

Re-registration preserves existing invoker restrictions, channel/category rules,
agent pins and enabled backends, adding only the selfhosted backend selection.

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
and saves/prints the error. The helper also requires IMDS curl timeouts from both pods.
HTTP 401, TLS failure, missing curl and DNS failure do
not count as blocked egress. This explicit negative probe happens after the turn
log snapshot because it intentionally introduces a forbidden-domain lookup.
Do not claim egress proof if any other NetworkPolicy admits broad harness egress.

### TEARDOWN and reinstall (target: under 30 minutes)

For a disposable pilot, `teardown` removes the Helm release, namespace **including
both PVCs and their data**, AKS cluster and managed node RG. Confirm backups or
accept loss of all tenants, ledger, sessions and workspaces before running it.
It keeps **ACR/images and AOAI/gpt-5.4-mini deployment**, plus the protected operator state;
ACR/storage and AOAI usage remain billable. It checks the managed node RG was
removed. An incomplete deletion is a failed teardown, not a passing result.

```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh teardown
```

The helper verifies its dedicated kubeconfig's API server before uninstalling
the release, deleting the namespace or deleting AKS. Use the `teardown` helper
above so the target check runs before any destructive command.

Retaining built images and model quota avoids rebuilds and model provisioning on
the timed path. In the same shell, start the clock before re-creating AKS, run the
same install, re-register the application state below, and stop only after the
first successful **Daimon CLI/Teams** gpt-5.4-mini reply. Record timestamps and elapsed
seconds. Thirty minutes is a target, **not measured Azure evidence** from this
lane; capacity, image pulls, RBAC propagation and application setup may exceed it.

```sh
date -u +%Y-%m-%dT%H:%M:%SZ > "$AKS_STATE/reinstall-start"
time bash deploy/helm/daimon-selfhost/scripts/aks.sh reinstall
bash deploy/helm/daimon-selfhost/scripts/aks.sh register
export MESSAGE='Reply only: REINSTALLED.'
bash deploy/helm/daimon-selfhost/scripts/aks.sh turn
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

## Register and run a real selfhosted turn

Build both images from the reviewed `azure-selfhost` source containing the
production selfhosted CLI turn and the verified C3.9a MCP CA-bundle client.
These commands use release `coke`, CLI caller `operator`, channel
`selfhosted-operator`, and persistent CLI thread `aks-proof`.

The following is an explicit **TEST tariff**, not a quote of Azure's prices.
For production, replace the accounting name/rates/date with your verified contract
rates and set `ALLOW_TEST_TARIFFS=false`. Infrastructure is explicitly zero here and funded separately. Registration
creates a tenant with $2 signup credit and the default scoped agent/environment,
an access policy permitting selfhosted (the schema also requires Anthropic), and the selfhosted channel configuration revision.
It runs `daimon selfhosted init`, then writes the resulting
agent/environment IDs and configuration digest into the provider grant Secret.
It makes no model calls. Retain these exports for reinstall:

```sh
export SELFHOSTED_MODEL=gpt-5.4-mini
export ALLOW_TEST_TARIFFS=true
export SELFHOSTED_MODELS='{"gpt-5.4-mini-TEST":[{"effective_from":"2026-10-21","input":"0.75","output":"4.5","cache_read":"0.075"}]}'
bash deploy/helm/daimon-selfhost/scripts/aks.sh register
bash deploy/helm/daimon-selfhost/scripts/aks.sh egress-start
export MESSAGE='Use bash to print the working directory, then write azure-proof.txt containing PERSISTED. Confirm success.'
bash deploy/helm/daimon-selfhost/scripts/aks.sh turn
export MESSAGE='Read azure-proof.txt from the previous turn and report its exact contents.'
bash deploy/helm/daimon-selfhost/scripts/aks.sh turn
bash deploy/helm/daimon-selfhost/scripts/aks.sh egress-check
```

`register` is implemented in [`scripts/register.sh`](scripts/register.sh) and
[`scripts/provision.py`](scripts/provision.py), the lane-15 production setup flow.
It sets `DAIMON_TURN__PATH=mux`, channel backends and selfhosted admission on;
sets billing `DEPLOYMENTS`, `MODELS` and explicit `ALLOW_TEST_TARIFFS`; and binds the host Secret's
`DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY` to the exact
`https://coke-daimon-harness:8088` origin and created tenant. The same token is used by the harness. `daimon-host` must contain an actual key
named `DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY`; host pods load it with `envFrom`.
Do not put the value in Helm values or substitute a `secretKeyRef` object inside
`host.env` (it accepts string values). The installer adds this key to saved
legacy `host.env` state from the retained harness token without printing it.
It configures credential-bearing MCP at the internal
`https://coke-daimon-mcp:8765/mcp` URL on host and harness, with the chart CA.
Registration sets the advertised MCP resource URL to this internal address;
this is a **CLI-only pilot**. Before enabling external OAuth/MCP clients, configure
the public resource URL and a separately reviewed internal routing/trust path.
The host trusts the chart CA through `SSL_CERT_FILE`; the harness MCP client uses
its scoped bundle. Both bearer hops use HTTPS, without plaintext consent grants.

`host.runtimeSecret=daimon-runtime` mounts only `runtime.json` read-only at
`/etc/daimon-runtime/runtime.json` and sets `DAIMON_TURN__PROVIDER_RUNTIME_FILE`.
The manifest contains IDs/grants, never bearer or model keys. Runtime and CSI
mounts compose. Registration applies the Secret and upgrades the release, then
restarts MCP/scheduler so cached grants refresh. Admission defaults off in the
chart; registration enables it only after the records and grant are present.

`turn` uses the production `admit → bind_session → run_prepared_turn` CLI path with real
billing and no exemption. Save its NDJSON (usage IDs and session IDs), the harness
provider receipts and matching Postgres usage/ledger rows for independent token
reconciliation. The direct `probe` helper is an optional paid C3 diagnostic and
does not replace these two application turns. No real inference is performed by
this lane's local tests. The recipe uses gpt-5.4-mini for every model turn.

## Local validation

Linux amd64, Docker, uv, Python 3 and curl are required. Binaries are pinned and
SHA256-verified against the upstream release checksum files, installed in
`~/.local/bin`. Build from the reviewed application source; the smoke test
exercises its CLI/HTTPS path. No Azure credentials or resources
are used:

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

The render regression suite checks runtime/CSI composition, file path and memory
limits, probes/grace period, priorities, Graph routes, ACR Postgres paths,
default egress destinations, external PG exceptions and the policy switch:

```sh
uv run python -m unittest discover -s deploy/helm/daimon-selfhost/tests -p 'test_*.py'
```

The script loads images and post-renders only offline fixtures: a 20-second
Postgres startup delay and the fake harness adapter. It installs PG, the init
Job and all host processes together with `--wait --wait-for-jobs`, asserts revision
one is deployed with no main-container restarts, and verifies all serving pods
Ready, MCP health, authenticated C3 health, unauthorized rejection, two sessions,
tool writes, fake usage, event polling, idempotency and replay after pod replacement.
It saves the original events in the MCP pod, compares GET replay before any POST,
checks execution counts, and requires the exact `persisted` tool-result content
from new read turns. Deleting the file must fail that check even if the turn
completes. Both Services use real HTTPS; MCP is checked through the harness
client as UID 10000, and both endpoints reject clients without the chart CA.
The test checks that Helm upgrade retains the CA. It verifies the multi-mode
proc/scratch checks, service UID 10000, SYS_ADMIN bounding-set removal and unprivileged tool UID.
Only inference is replaced. The fixture invokes the production app factory with
no test mode: credential-source/file scans, multi-session validation, UID drop
and process protection all run. F80 checks the CA/TLS mounts with the production
credential-file validator, then makes MCP HTTPS calls through the production
client from the actual service UID 10000 after the drop. The CA remains unreadable
to tool UIDs. This development runtime injects host DMI identity files into nested
kind pods. The offline post-renderer masks only `/sys/devices/virtual/dmi/id`
with an empty read-only mount after the lab OCI hook runs, before dropping startup
SYS_ADMIN. It does not skip or filter the production scanner,
and the chart CA/TLS mounts remain present and scanned. The smoke requires both F80 receipts before and after registration.
It runs kubeconform when
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

# Daimon on Azure — Coke IT pilot

**Scope:** ACR Basic, one-node AKS (`Standard_D2s_v6`, 2 vCPU / 8 GiB), Azure
OpenAI. PostgreSQL and persistent agent workspaces run in AKS. No ACA, Key Vault,
Blob or PostgreSQL Flexible Server. Follow the [full recipe](README.md#aks-recipe--empty-resource-group-to-a-running-pilot)
for checks and recovery; scripts execute its commands without printing secrets.

**Before starting:** dedicated RG and budget alerts; Azure CLI login, git checkout,
Helm, kubectl and Python 3. Deployer: RG Owner, or Contributor plus role-assignment
rights for ACR attachment. Subscription admin must register ContainerService,
ContainerRegistry, CognitiveServices, Compute, Network and Storage providers.
Confirm Dsv6 quota/capacity and **Luna DataZoneStandard quota** in `eastus2` (US)
or an approved EU Data Zone region. Sol quota is needed for later production.
Approve the harness PodSecurity exception and cluster-admin DNS/probe operations.
An existing Bot/Entra app, HTTPS ingress and tenant approval are separate prerequisites
for Teams; the initial CLI/harness pilot disables Teams.

**Install from the reviewed repository root** (replace globally unique names):

```sh
export RG=daimon-selfhost LOCATION=eastus2
export ACR=daimoncoke12345 AKS=daimon-coke AOAI=daimon-coke-12345
export TAG=$(git rev-parse HEAD)
export AKS_STATE=$(mktemp -d)
bash deploy/helm/daimon-selfhost/scripts/aks.sh bootstrap
bash deploy/helm/daimon-selfhost/scripts/aks.sh install
bash deploy/helm/daimon-selfhost/scripts/aks.sh egress-start
bash deploy/helm/daimon-selfhost/scripts/aks.sh probe
bash deploy/helm/daimon-selfhost/scripts/aks.sh egress-check
```

`bootstrap` builds both images in ACR, mirrors PG, creates Azure-policy AKS with
ACR attachment, and deploys Luna. `install` creates Kubernetes Secrets and uses one Helm wait for PG, migrations
and all serving pods. The Postgres image is explicitly imported into ACR. `probe` makes paid Luna-only C3 tool/file turns; it is **not**
a CLI/Teams ledger test. Register the tenant/channel/runtime grant in the recipe
before testing the actual Daimon entry point. Keep `AKS_STATE` private: it contains
operator keys. DNS logs and the blocked Anthropic curl provide egress evidence.

**Security:** application data/workspaces stay in the tenant's cluster; inference
uses Azure OpenAI in the approved US/EU Data Zone. API key is an explicit pilot
Secret, captured/scrubbed by the harness. Production identity needs the reviewed
Workload Identity/header-protected proxy. Sessions share a kernel and node:
trusted users only, no Kata/per-session VM isolation, HA, automated DB backup or
PITR. Default requests: 650m CPU / 1.875 GiB; host limits: 1536 MiB each (minimal recipe: 7.5 GiB total limits). AKS also creates
a billable node RG with VM, disks, IP and networking. No production SLA is implied.

**Cost:** allow roughly $100–170/month continuously running, plus tokens, builds
and egress; unverified planning range, confirm region/contract prices in the recipe.

**Remove / reinstall:** these pilot resets destroy DB, ledger and workspace data.
Teardown deletes AKS/PVCs and keeps billable ACR and AOAI; reinstall reuses them,
targeting <30 minutes to the first real application reply (not yet measured).

```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh teardown
time bash deploy/helm/daimon-selfhost/scripts/aks.sh reinstall
bash deploy/helm/daimon-selfhost/scripts/aks.sh remove
rm -r "$AKS_STATE"
```

Use `remove` only for complete removal: it deletes the dedicated RG and budget.
Verify node RG/disks are gone. Retain backups first; never publish operator state.

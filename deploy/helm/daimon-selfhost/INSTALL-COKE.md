# Daimon on Azure — Coke IT install kit

**Pilot:** one AKS node, ACR Basic and Azure OpenAI (AOAI); PostgreSQL, session workspaces, MCP server and scheduler run in the cluster. Teams is off by default.
Full operator instructions: [AKS recipe](README.md#aks-recipe--empty-resource-group-to-a-running-pilot).

**Before starting:** use a dedicated resource group and budget; Azure CLI logged
into the target subscription, Helm 3.19+, kubectl, Python 3, git; reviewed `azure-selfhost` checkout.
Resource-group Owner, or Contributor plus ACR role-assignment rights, is required; subscription admin registers **Microsoft.ContainerService, Microsoft.ContainerRegistry,
Microsoft.CognitiveServices, Microsoft.Compute, Microsoft.Network, Microsoft.Storage**.
Cluster-admin access and approval for the harness PodSecurity exception are required.
Confirm Dsv6 CPU quota/capacity for one **Standard_D2s_v6** node (pilot minimum);
**Standard_D4s_v6** is recommended for production. AKS system pools reject B-series.
Use **eastus2 (US Data Zone)** or an approved EU Data Zone region; obtain a supported AOAI deployment (**gpt-5.4-mini**, DataZoneStandard) and regional model quota.
Existing AOAI is reused; set its account/deployment names below. Processing stays
within the selected US/EU zone, not necessarily one region. AOAI local authentication
is required for this API-key install; disabling it requires a reviewed identity proxy.

**Install:** run from the repository root in one shell. Replace globally unique
ACR/AOAI names; use the existing AOAI account's resource group when reusing it.
Keep the generated `AKS_STATE` directory private: it contains keys. Never enable shell tracing.
```sh
export RG=daimon-selfhost LOCATION=eastus2
export ACR=daimoncoke12345 AKS=daimon-coke AOAI=daimon-coke-12345
export AOAI_DEPLOYMENT=gpt-5.4-mini; export TAG=$(git rev-parse HEAD)
export AKS_STATE=$(mktemp -d)
bash deploy/helm/daimon-selfhost/scripts/aks.sh bootstrap
bash deploy/helm/daimon-selfhost/scripts/aks.sh install
bash deploy/helm/daimon-selfhost/scripts/aks.sh probe
```
Builds/pushes both images, mirrors PostgreSQL, creates AKS/network/disks/public IP,
and creates AOAI if absent. Helm installs Secrets, in-cluster TLS, policies and
persistent volumes; waits for migrations and Ready pods. **Probe is a paid preflight:** two short
**gpt-5.4-mini** turns write a file, then read it back; model tokens are billed.
For registration, application turns and egress checks, follow [Register and run](README.md#register-and-run-a-real-selfhosted-turn); sample billing rates are TEST rates.

**Cost:** observed D2s_v6 compute ≈ **US$2.30/day per node** (two ≈ $4.60/day), plus AOAI usage, ACR Basic/storage/builds, disks, public IP and network traffic.
AKS Free control-plane tier has no SLA; nodes are billed. Confirm your region/contract
and set budget alerts before installing; these observations are not a quote.

**Security:** database, ledger and workspaces stay in your tenant; inference goes
to your Azure OpenAI deployment. MCP and harness use verified in-cluster HTTPS
with a chart-generated CA; keys use Kubernetes Secrets and protected operator files.
Default-deny host/harness egress permits DNS and required same-release services;
harness AOAI HTTPS is pinned to endpoint IPv4 /32s. Recheck pins when DNS changes.
No Anthropic or OpenAI.com calls are allowed. Validate enforcement with the recipe's
egress checks. Sessions share a kernel: trusted tenant only, no per-session VM boundary. One node has no HA; bundled PostgreSQL has no automatic backup or PITR.

**Reset (irreversible):** confirm backups or accept loss of all tenants, database,
ledger, sessions and workspaces. Teardown deletes the release, namespace, both PVCs,
AKS and managed node resource group; verifies its deletion; retains billable ACR/images,
AOAI/deployment and protected operator state. Reinstall targets 30 minutes, not measured.
```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh teardown
```
**Remove everything:** deletes the dedicated group, budget, ACR and AOAI (even reused).
Verify no node/data-disk group, disks or IPs remain before saying billing has stopped.
```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh remove
rm -r "$AKS_STATE"
```

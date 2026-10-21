# Daimon on Azure — Coke IT install kit

**Pilot:** one AKS node, ACR Basic and Azure OpenAI (AOAI); PostgreSQL, workspaces, MCP and scheduler run in the cluster. Teams is off. [Repository](https://github.com/pymc-labs/daimon), reviewed `azure-selfhost` branch; [full AKS recipe](README.md#aks-recipe--empty-resource-group-to-a-running-pilot).

**Before starting:** use a DEDICATED Daimon resource group and budget, never a shared
AOAI group. Reuse AOAI only if already in this dedicated group; otherwise create new.
The helper cannot reuse AOAI from another group. Removal deletes everything in `RG`.
Use Linux/WSL; Azure CLI must support az acr run and az acr manifest show; subscription policy must allow ACR Tasks.
Helm 3.19+, kubectl, Python 3 and git are required; no local Docker/buildx is needed.
RG Owner, or Contributor plus ACR role-assignment rights; cluster-admin is required.
A subscription admin registers **Microsoft.ContainerService, Microsoft.ContainerRegistry,
Microsoft.CognitiveServices, Microsoft.Compute, Microsoft.Network, Microsoft.Storage**.
Approve privileged PodSecurity for the whole `daimon` namespace, not only the harness.
Confirm Dsv6 CPU quota/capacity: **Standard_D2s_v6** pilot minimum, **Standard_D4s_v6**
recommended for production; no B-series system pools. Use **eastus2 (US Data Zone)** or approved EU Data Zone; obtain **gpt-5.4-mini**, DataZoneStandard deployment/quota.
Inference stays in that US/EU zone, not necessarily one region. AOAI local auth is required for this API-key path; disabling it requires a reviewed identity proxy.

**Install:** from the repository root, one shell; replace globally unique ACR/AOAI names. Keep `AKS_STATE` private (contains keys); retain it for reinstall. No shell tracing.
```sh
export RG=daimon-selfhost LOCATION=eastus2
export ACR=daimoncoke12345 AKS=daimon-coke AOAI=daimon-coke-12345
export AOAI_DEPLOYMENT=gpt-5.4-mini
export AKS_STATE=$(mktemp -d)
bash deploy/helm/daimon-selfhost/scripts/aks.sh bootstrap
bash deploy/helm/daimon-selfhost/scripts/aks.sh install
bash deploy/helm/daimon-selfhost/scripts/aks.sh probe
```
Bootstrap saves its image tag in `AKS_STATE`; uploads source to ACR Tasks. Remote BuildKit builds/pushes both images (default wait: 2 hours); pulls pinned Docker Hub frontend **docker/dockerfile:1.7.0**.
Postgres import tries **ECR Public first, Docker Hub fallback**, pinned to the reviewed digest; verifies the index, Linux/amd64 descriptor and child manifest in ACR.
Import has a **15-minute total budget** (3-minute attempt cap); failure stops before AKS creation. Bootstrap then creates AKS/network/disks/public IP and AOAI if absent.
Helm installs Secrets, TLS, policies, PVCs; waits for migrations/Ready pods. **Probe
is paid:** two short **gpt-5.4-mini** turns write then read a file; tokens are billed.
**App admission stays off until [Register and run](README.md#register-and-run-a-real-selfhosted-turn)**; follow its registration, application turns and egress checks. Sample billing rates are TEST rates.

**Cost:** observed node compute ≈ **US$2.30/day (~$70/month)**. Plan **$100–170/month**
for one pilot including disks/network/ACR, plus model tokens/builds/egress; unverified
planning range, not a quote. Confirm region/contract and budgets. AKS Free has no SLA.

**Security trade-offs:** public AKS API and public AOAI endpoint (no Private Endpoint),
public-IP node egress; ECR Public/Docker Hub import sources and Docker Hub pulls (curl probe, BuildKit frontend). Public mirrors can rate-limit; no extra pod egress is allowed.
Kubernetes Secrets/local key files replace Key Vault. The whole namespace permits
privileged PodSecurity. Sessions share a kernel: trusted tenant only, no per-session VM.
One node has no HA; bundled PG has no automatic backups/PITR. Data stays in your tenant;
inference uses AOAI. MCP/harness use verified chart-CA HTTPS. Default-deny egress pins
AOAI IPv4 /32s; refresh when DNS changes. Validate recipe egress checks: no Anthropic/OpenAI.com.

**Reset (irreversible):** confirm backups or accept loss of all tenants, ledger, sessions
and workspaces. Teardown deletes release, namespace, both PVCs/data, AKS and managed node
RG; verifies deletion. Retains billable ACR/images, AOAI/deployment and protected state.
Reinstall independently measured: about **7 minutes from cluster create to first reply**; follow the recipe's register/turn steps after reinstall to restore application access.
```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh teardown
time bash deploy/helm/daimon-selfhost/scripts/aks.sh reinstall
```
**Remove everything:** deletes all of `RG`, including budget, AKS, ACR and reused AOAI.
Verify no node/data-disk RG, disks or IPs remain before saying billing has stopped.
```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh remove
rm -r "$AKS_STATE"
```

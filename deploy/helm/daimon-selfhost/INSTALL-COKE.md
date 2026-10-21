# Daimon on Azure — Coke IT pilot

**Scope:** ACR Basic, one-node AKS, Azure OpenAI. PostgreSQL and workspaces run
in AKS; no ACA, Key Vault, Blob or Flexible Server. D2s_v6 is the minimum test
size; Standard_D4s_v6 is recommended for production. Follow the [recipe](README.md#aks-recipe--empty-resource-group-to-a-running-pilot).

**Prerequisites:** dedicated RG/budget; Azure CLI login, Helm, kubectl, Python 3;
reviewed checkout containing PR #1276 for in-cluster HTTP. RG Owner, or Contributor
plus ACR role-assignment rights. Subscription admin registers ContainerService,
ContainerRegistry, CognitiveServices, Compute, Network and Storage. Confirm Dsv6
capacity and **Luna DataZoneStandard quota** in eastus2 or an approved EU region;
Sol quota is needed for production. Approve the harness PodSecurity exception and
cluster-admin DNS/probes. Teams additionally needs Bot/Entra credentials, tenant
approval and HTTPS ingress; initially disabled. AOAI **disable local auth** policy
blocks this API-key path: a reviewed MI proxy is then required before installation.

**Install** from the reviewed repository root; replace globally unique names:
```sh
export RG=daimon-selfhost LOCATION=eastus2
export ACR=daimoncoke12345 AKS=daimon-coke AOAI=daimon-coke-12345
export TAG=$(git rev-parse HEAD)
export AKS_STATE=$(mktemp -d)
bash deploy/helm/daimon-selfhost/scripts/aks.sh bootstrap
bash deploy/helm/daimon-selfhost/scripts/aks.sh install
export SELFHOSTED_MODEL=gpt-6-luna
export ALLOW_TEST_TARIFFS=true
export SELFHOSTED_MODELS='{"gpt-6-luna-TEST":[{"effective_from":"2026-10-21","input":"0.75","output":"4.5","cache_read":"0.075"}]}'
bash deploy/helm/daimon-selfhost/scripts/aks.sh register
bash deploy/helm/daimon-selfhost/scripts/aks.sh egress-start
export MESSAGE='Use bash to print the working directory, then write azure-proof.txt containing PERSISTED. Confirm success.'
bash deploy/helm/daimon-selfhost/scripts/aks.sh turn
export MESSAGE='Read azure-proof.txt from the previous turn and report its exact contents.'
bash deploy/helm/daimon-selfhost/scripts/aks.sh turn
bash deploy/helm/daimon-selfhost/scripts/aks.sh egress-check
```

Builds/mirrors precede overlay-network AKS and Luna deployment. Install waits for
PG/migrations/pods; registration funds a tenant and sets channel/runtime/billing.
Rates above are explicit **TEST rates** with opt-in; replace and disable for production.
Keep `AKS_STATE` private. Egress proof checks DNS and blocked curls in both pods.

**Security:** data/workspaces stay in the tenant cluster; inference uses Azure
OpenAI's approved US/EU Data Zone. MCP uses chart-CA TLS; pilot keys use Secrets. Production
needs the reviewed MI proxy. Shared kernel/node: trusted users only; no per-session
VM isolation, HA, automatic DB backup or PITR. Minimal requests: 250m / 1408 MiB;
observed D2 allocatable: 1900m / 5.66 GiB. Memory limits overcommit; bound concurrency.

**Cost:** $100–170/month unverified pilot allowance plus tokens/builds/egress; confirm actual prices. AKS creates a billable node RG.

**Reset/remove:** destroys DB/ledger/workspaces. Teardown retains billable ACR/AOAI;
reinstall reuses them, targeting <30 minutes to a real reply (not measured here).
```sh
bash deploy/helm/daimon-selfhost/scripts/aks.sh teardown
time bash deploy/helm/daimon-selfhost/scripts/aks.sh reinstall
bash deploy/helm/daimon-selfhost/scripts/aks.sh register
export MESSAGE='Reply only: REINSTALLED.'
bash deploy/helm/daimon-selfhost/scripts/aks.sh turn
bash deploy/helm/daimon-selfhost/scripts/aks.sh remove
rm -r "$AKS_STATE"
```

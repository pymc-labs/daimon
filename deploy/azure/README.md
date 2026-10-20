# Azure deployment

Offline-validated Bicep for the current root Dockerfile. Nothing here has been
applied to an Azure subscription. Compilation checks resource schemas; it does
not establish regional availability, quota, RBAC propagation or runtime health.

## Resources

`main.bicep` targets an existing resource group. `azd/main.bicep` wraps it in a
subscription deployment that creates `rg-<namePrefix>-<environmentName>`.
The root `azure.yaml` uses that wrapper and provisions infrastructure only;
it has no automatic build/deploy hooks.

| Resource | Configuration |
| --- | --- |
| Container Apps environment | Consumption profile, Log Analytics integration |
| Serving apps | Discord, Teams, MCP and scheduler, each 0.5 vCPU / 1 GiB, exactly one replica, 90-second shutdown grace |
| Init | Manual Container Apps Job, same image, migrations followed by defaults; no automatic execution or retries |
| PostgreSQL | Flexible Server, PostgreSQL 17, Burstable B1ms, 32 GiB with autogrow, seven-day backup, no HA |
| Key Vault | RBAC, optional purge protection (off for dev), 90-day soft delete; database URL stored as a secret |
| Storage | LRS Blob, private `charts` container, versioning and seven-day soft delete, shared keys disabled |
| ACR | Basic, admin credentials disabled, image pulls via managed identity |
| Logs | 30-day retention, 1 GiB/day ingestion cap (enforcement can lag) |
| AI | Optional (`deployAi=true` by default): AIServices account, GPT-5 deployment, `daimon` Foundry project with system identity |

The `services` array defaults to `['discord', 'teams', 'mcp', 'scheduler']`.
For a Discord-only install, use `['discord', 'mcp', 'scheduler']`; for Teams only,
use `['teams', 'mcp', 'scheduler']`. Only selected apps and their platform secret
references are created. MCP receives the selected gateways' credentials and
inherits `serviceEnvironment.teams` when Teams is selected, so channel tools
register with the same platform settings. Per-service overrides still apply.
Removing a service from the array does not stop/delete an existing app in an
incremental deployment.

Discord and scheduler have HTTP liveness probes on their event-loop responders,
ports 8081 and 8082 respectively. These ports have no public ingress.
MCP (8765) and Teams (3978) have public HTTPS ingress; Discord and scheduler
have none. Serving commands override the image entrypoint, matching Compose:
seeding belongs to the init job. No notebook/report host, Slack, Teams Bot
registration, harness image or hosted-agent version is deployed by this stack.

`enablePrivateNetworking` accepts **false only**. This baseline uses public
service endpoints. Postgres allows Azure-service connections through its
`0.0.0.0` firewall exception, which admits connections from other Azure tenants
as well; authentication and TLS are still required. This is not tenant sealing.
Optional `postgresFirewallRules` add named operator IP ranges. There are no
private endpoints, VNet isolation or egress controls in these templates.

## Validate offline

Requires Python 3, Bash and the standalone Bicep CLI; no Azure CLI or credentials.
Install from the [official Bicep releases](https://github.com/Azure/bicep/releases).
The templates were validated with v0.48.1 on Linux x64:

```bash
mkdir -p "$HOME/.local/bin"
curl -fsSL https://github.com/Azure/bicep/releases/download/v0.48.1/bicep-linux-x64 \
  -o "$HOME/.local/bin/bicep"
echo 'b09ec25a9d376c1f8e33ede6ed22b587f915ad68488d5db77a6f9541748c7f6e  '"$HOME/.local/bin/bicep" | sha256sum --check
chmod +x "$HOME/.local/bin/bicep"
export PATH="$HOME/.local/bin:$PATH"
scripts/azure/validate.sh
```

The script builds and lints every module and both entrypoints, fails on warnings
as well as errors, and removes generated ARM JSON from its temporary directory.
It never invokes Azure. For individual checks:

```bash
bicep build deploy/azure/main.bicep --outfile /tmp/daimon-azure.json
bicep lint deploy/azure/main.bicep
```

## Subscription prerequisites

Run the following sections only once a subscription exists and deployment is
authorized. Install Azure CLI (including the `containerapp` extension), Azure
Developer CLI (`azd`), Docker with BuildKit, and Bicep first.

- Register `Microsoft.App`, `Microsoft.OperationalInsights`,
  `Microsoft.ManagedIdentity`, `Microsoft.ContainerRegistry`,
  `Microsoft.KeyVault`, `Microsoft.Storage`, `Microsoft.DBforPostgreSQL`,
  and `Microsoft.CognitiveServices` resource providers.
- Confirm the chosen region supports B1ms PostgreSQL, Container Apps,
  AIServices projects, the requested GPT-5 model version/SKU, and hosted agents.
  Region support for one does not imply the others. Check the
  [model availability table](https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-models/concepts/models-sold-directly-by-azure)
  and [hosted-agent prerequisites](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent).
- Obtain GPT-5 quota for **that region and deployment SKU**, sufficient for
  `gpt5Capacity`. Its units are Azure model quota units, not a universal TPM
  multiplier. Check Container Apps consumption CPU quota (at least 2.5 vCPU
  for four serving replicas plus init), PostgreSQL availability and ACR quota.
- Group deployment needs resource creation permissions plus role-assignment
  creation at the group: Owner, or Contributor + Role Based Access Control
  Administrator. The azd wrapper additionally needs subscription-level rights
  to create a resource group and deploy into it. Provider registration may
  require subscription-level permissions even for an existing-group install.
- The operator writing secrets needs **Key Vault Secrets Officer** on the
  generated vault; Contributor alone has no secret data access. The image
  publisher needs **AcrPush** on the registry. Grant these separately to the
  operator identity; the runtime identity is never given them.
- Hosted-agent deployment is a separate step. The deploying principal needs
  Foundry Project Manager at the project and any permissions needed to assign
  the platform-created agent identity; see the
  [hosted-agent permissions reference](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-permissions).

The runtime user-assigned identity receives only AcrPull on this registry,
Key Vault Secrets User on this vault, Storage Blob Delegator on this storage
account, Storage Blob Data Contributor on the `charts` container, and Cognitive
Services OpenAI User on the AI account. The account-scoped Blob role only
permits delegation-key issuance; blob data access stays scoped to charts.
The project's system identity also receives Cognitive Services OpenAI User on
the AI account. No subscription-wide runtime roles are assigned. Hosted-agent
identities and their inference, registry and invocation access are configured
when that agent is deployed, not by these templates.

## Provision the foundation

Choose lowercase letters/digits for `namePrefix` (3–8 characters), starting
with a lowercase letter. Resource names include a deterministic group-specific
suffix. Keep the same group, prefix and location for later stages.

Set `vaultPurgeProtection=true` for M3/Coke and other production installs.
The default false supports development teardown; once enabled on an existing
vault, Azure will not let it be disabled. Deleting a vault reserves its name
until recovery or purge; with protection enabled it cannot be purged for the
90-day retention period. After teardown, recover the old vault or use a new
environment name/group or `namePrefix`; do not assume the same deterministic
name can be recreated. An unprotected development vault still needs recovery
or an authorized purge before reusing its name. AI account soft deletion can
also reserve the custom subdomain; recover/purge it or rename the deployment.

If GPT-5 quota/entitlement is pending, set `deployAi=false` in your local
parameter file **before** the first provision. This omits the entire AI module
and returns empty AI endpoint/deployment/project outputs. The existing-backend
M1 foundation can then provision without AI quota. Keep other parameters stable
and rerun with `deployAi=true` after approval to add AI resources. Turning it
false later does not delete already deployed AI resources.

For azd, from the repository root:

```bash
az login
azd auth login
azd env new azure-dev
azd env set AZURE_LOCATION eastus2
# For M1 without GPT-5 quota, first add "deployAi": {"value": false}
# under parameters in deploy/azure/azd/main.parameters.json (local edit only).
azd up
```

`azd` prompts for the required secure `postgresAdministratorPassword`. Use a
password with upper/lowercase letters, digits, and a URL-safe symbol such as
`-`, at least 16 characters. Keep it in your secret manager and supply the same
value for subsequent stages. Use only letters, digits, `-`, `_`, `.`, `~`:
the current Alembic configuration cannot interpolate percent-escaped passwords.
Do not put it in CLI arguments, committed files or `azd env set`.
The checked-in azd parameter file contains only environment name/location;
leave secure input to the prompt, or supply an ARM Key Vault parameter reference
through your deployment pipeline. Review any locally cached secure parameters
before sharing azd state.

Alternatively, target a group yourself. Create a local, untracked parameter
file with nonsecret settings and a reference to a password already held in a
bootstrap vault, using the actual resource ID and secret name:

```json
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "location": { "value": "eastus2" },
    "namePrefix": { "value": "daimon" },
    "postgresAdministratorPassword": {
      "reference": {
        "keyVault": { "id": "<bootstrap-vault-resource-id>" },
        "secretName": "postgres-admin-password"
      }
    }
  }
}
```

The bootstrap vault must permit template deployment and the deployer must have
`Microsoft.KeyVault/vaults/deploy/action` on it. It is separate from the vault
this stack creates. Keep parameter files outside the repository.

```bash
az account set --subscription '<subscription-id>'
az group create --name rg-daimon-azure-dev --location eastus2
az deployment group what-if --resource-group rg-daimon-azure-dev \
  --template-file deploy/azure/main.bicep --parameters @/secure/azure.parameters.json
az deployment group create --name daimon-foundation \
  --resource-group rg-daimon-azure-dev \
  --template-file deploy/azure/main.bicep --parameters @/secure/azure.parameters.json
```

Use either azd or the group route consistently. For the remaining commands,
`RG`, `ACR` and `VAULT` are the nonsecret group name, `registryName` and
`keyVaultName` outputs; `INIT_JOB` is `initJobName`. The group route exposes
these through `az deployment group show --name daimon-foundation -g "$RG"
--query properties.outputs`. The azd route also writes corresponding uppercase
outputs into local environment state.

## Configure and initialize

Grant the operator data-plane roles described above and wait for RBAC to
propagate. Supply secret **values from protected files** (no trailing newline),
using `--output none` so the CLI does not print them. Populate only the
selected gateways' platform secrets (MCP shares them):

```bash
az keyvault secret set --vault-name "$VAULT" --name mcp-jwt-secret \
  --file /secure/mcp-jwt-secret --output none
az keyvault secret set --vault-name "$VAULT" --name crypto-keys \
  --file /secure/crypto-keys --output none
az keyvault secret set --vault-name "$VAULT" --name discord-bot-token \
  --file /secure/discord-bot-token --output none
az keyvault secret set --vault-name "$VAULT" --name teams-client-secret \
  --file /secure/teams-client-secret --output none
```

`mcp-jwt-secret` is the shared signing secret. `crypto-keys` is a Fernet key
ring in the format described in [self-hosting](../../docs/self-hosting.md).
The database URL is written to Key Vault by the database module, with TLS
and hostname verification required; every process and init receives
`PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt` to load the image's system CA
bundle. No `sslrootcert` keyword is added to the SQLAlchemy URL. The URL is
never a deployment output. The administrator password is supplied only to PostgreSQL and this secure URL construction.

Build the **current checkout** using the root Dockerfile and publish it to ACR:

```bash
IMAGE_TAG=$(git rev-parse HEAD)
az acr login --name "$ACR"
docker build --platform linux/amd64 -t "$ACR.azurecr.io/daimon:$IMAGE_TAG" .
docker push "$ACR.azurecr.io/daimon:$IMAGE_TAG"
IMAGE_DIGEST=$(az acr repository show --name "$ACR" \
  --image "daimon:$IMAGE_TAG" --query digest --output tsv)
IMAGE="$ACR.azurecr.io/daimon@$IMAGE_DIGEST"
```

Add `containerImage` = `$IMAGE` to your local parameters; keep `deployServices`
false. When Teams is selected, add these **nonsecret** values under `serviceEnvironment.teams`:
`DAIMON_TEAMS__CLIENT_ID` and `DAIMON_TEAMS__TENANT_ID` for an existing single-tenant
Bot registration. Use port 3978. The code creates no Entra application or Bot
registration. An operator must set the Bot messaging endpoint to the Teams
app's HTTPS URL plus `/api/messages` once serving is enabled.

**M1 on the existing backend requires Anthropic configuration before init.**
Map `DAIMON_ANTHROPIC__API_KEY` to `anthropic-api-key` in the common `secretNames`
parameter and populate that secret before running the init job:

```bash
az keyvault secret set --vault-name "$VAULT" --name anthropic-api-key \
  --file /secure/anthropic-api-key --output none
```

Add this entry inside your parameter file's `parameters` object:

```json
"secretNames": {
  "value": { "DAIMON_ANTHROPIC__API_KEY": "anthropic-api-key" }
}
```

Without that key, the current image's defaults command can succeed while
skipping default agents/environments, and M1 cannot answer a turn. If the key
was added after init, redeploy the job with its secret reference and rerun init
before enabling services. The later selfhosted path has separate agent and
environment initialization and does not use this M1 provider configuration.

Additional provider credentials go through `secretNames` (all processes) or
`serviceSecretNames` (one process), as maps of environment-variable names to
Key Vault secret names. For example, an existing Anthropic deployment can map
`DAIMON_ANTHROPIC__API_KEY` to an operator-populated `anthropic-api-key` secret.
The infrastructure templates accept no Anthropic key by default; the M1
existing-backend runtime requires the mapping described above. Do not put secret values in
`appEnvironment` or `serviceEnvironment`, and do not duplicate environment
names between plain and secret maps. Custom `serviceSecretNames` are merged over built-in platform mappings.
MCP inherits selected gateways' token/secret-name overrides; it can also have
its own explicit overrides.

On the existing-group route:

```bash
az deployment group what-if -g "$RG" --template-file deploy/azure/main.bicep \
  --parameters @/secure/azure.parameters.json
az deployment group create --name daimon-init -g "$RG" \
  --template-file deploy/azure/main.bicep --parameters @/secure/azure.parameters.json
az containerapp job start --name "$INIT_JOB" --resource-group "$RG"
az containerapp job execution list --name "$INIT_JOB" --resource-group "$RG" \
  --query '[].{name:name,status:properties.status}' --output table
```

Wait for that execution to report `Succeeded`. A failed init must be repaired
before enabling services. The deployment itself does not wait for, start or
check the job. Azure deployments have no Compose-style dependency on job
completion. Run only one init execution at a time.

For azd stages, update `deploy/azure/azd/main.parameters.json` **locally** with the
same nonsecret parameters (`containerImage`, `serviceEnvironment` and later
`deployServices`), then run `azd provision`; do not commit installation-specific
edits. Keep the secure password input or Key Vault reference on each invocation.

## Enable services

Set `deployServices` true in the same parameter file and run the same group
what-if/create commands (or `azd provision`). Do this only after init succeeds.
Verify all four replicas are healthy, MCP `/healthz` responds, and the Bot
registration points to Teams. Configure the desired provider using its existing
Daimon setup procedure. Provisioning an AI account alone does not switch Daimon's
backend or prove a turn works. There is no persistent `/app/runtime` mount in
this baseline; the existing OpenAI hosted-backend runtime-file setup needs
additional storage/mount configuration if used. The selfhosted driver uses its
separate environment/agent records and harness deployment.

For upgrades, stop traffic and serving processes before a migration that
requires exclusive access. Run init once with the new image, verify it succeeds,
then update the serving apps. Setting `deployServices=false` in an incremental
ARM deployment **does not delete or stop existing apps**. Stop them explicitly;
do not use this flag as an upgrade drain or rollback mechanism.

## Outputs and handoff

`daimonEnvironment` contains nonsecret shared settings, including the MCP public
URL and managed identity client ID. `daimonSecretNames` maps database URL, signing
secret and crypto key-ring environment variables to vault names. Apply per-service
maps from your parameters as well. `mcpArtifactsEnvironment` contains the four
nonsecret artifact settings applied only to MCP, using the foundation outputs.
The azd wrapper exposes the same map as `DAIMON_MCP_ARTIFACTS_ENVIRONMENT`.
`harnessEnvironment` contains:

- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` (the parameterized GPT-5
  deployment name) and `AZURE_OPENAI_API_VERSION`.
- `AZURE_CLIENT_ID` for the user-assigned identity and `AZURE_AI_PROJECT_ENDPOINT`.

With `deployAi=false`, AI endpoints/deployment names and `foundryProjectId`
are empty strings. With AI enabled, no Azure OpenAI API key is produced: local authentication is disabled; attach
an inference-authorized managed identity to the harness. The Foundry project
resource ID is also an output. Hosted-agent image deployment, its platform
identity permissions and its invocation endpoint are separate from this stack.

## Chart storage

The MCP app is configured automatically for private Azure Blob chart links:

| Environment variable | Template value |
| --- | --- |
| `DAIMON_ARTIFACTS__BACKEND` | `azure_blob` |
| `DAIMON_ARTIFACTS__ENDPOINT_URL` | Storage account Blob endpoint |
| `DAIMON_ARTIFACTS__BUCKET` | Provisioned `charts` container name |
| `DAIMON_ARTIFACTS__AZURE_CLIENT_ID` | Runtime user-assigned identity client id |

These values come from the foundation module's outputs. The artifact settings
are applied only to MCP; other services and init do not receive them by default.
`serviceEnvironment.mcp` can override them using the existing per-service
configuration mechanism. Keep S3 keys and Azurite connection strings unset.

The runtime identity receives **Storage Blob Delegator** at storage-account
scope to mint user-delegation keys, plus **Storage Blob Data Contributor**
scoped to the charts container for uploads and reads. There is no account-wide
Blob data role. See the [Azure storage role definitions](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/storage).
The account disables shared keys and anonymous access. Links use read-only,
per-blob HTTPS user-delegation SAS; `ManagedIdentityCredential` uses the explicit
artifact client id with no developer-login or service-principal fallback.

URL expiry defaults to ten minutes. Optional overrides belong in
`serviceEnvironment.mcp`, for example `DAIMON_ARTIFACTS__URL_TTL_SECONDS` or
`DAIMON_ARTIFACTS__EMBED_IMAGES`. Expiry does not remove blobs; versioning and
soft delete do not impose an object-retention lifecycle or replace a backup.
Plan retention and cleanup before storing artifacts. Offline tests validate
wiring and role scopes; live identity, RBAC propagation and signed-download
verification remain deployment checks.

## Monthly budget

Planning estimate in USD for 730 hours, pay-as-you-go, a small US-region
installation, before tax/discounts. These are estimates, not a subscription
quote; confirm prices in the selected region before deploying.

| Item | Estimated monthly cost |
| --- | ---: |
| Four ACA replicas, aggregate 2 vCPU / 4 GiB, billed active throughout | $150–170 |
| B1ms PostgreSQL plus 32 GiB storage, light backup use | $20–35 |
| Basic ACR | $5–6 |
| Log Analytics, light ingestion below 5 GiB/month | $0–15 |
| Blob and Key Vault, light storage/operations | $1–5 |
| Baseline subtotal | **$176–231/month** |

ACA can charge less for eligible idle replicas; gateway/scheduler processes
cannot simply scale to zero. Init-job compute, network egress, hosted-agent
session compute and GPT-5 tokens are additional usage charges. Capacity on a
Standard model deployment is quota, not a prepaid token allowance. GlobalStandard
can route inference outside the resource region; it is not a tenant/region
residency guarantee. Select a supported SKU consistent with the intended policy.

Sources: [ACA pricing](https://azure.microsoft.com/en-us/pricing/details/container-apps/),
[PostgreSQL pricing](https://azure.microsoft.com/en-us/pricing/details/postgresql/flexible-server/),
[ACR pricing](https://azure.microsoft.com/en-us/pricing/details/container-registry/),
[Log Analytics pricing](https://azure.microsoft.com/en-us/pricing/details/monitor/),
[Azure OpenAI pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/).

import { defaultModelVersion } from './models.bicep'
import { validateNamePrefix } from './names.bicep'
import { validateChartStorage, chartArtifactsEnvironment } from './chart-storage.bicep'

targetScope = 'resourceGroup'

param location string = resourceGroup().location

@minLength(3)
@maxLength(8)
@description('Lowercase letters and digits, starting with a letter; used in globally unique resource names.')
param namePrefix string = 'daimon'

param modelName string = 'gpt-6.1-sol'
param modelDeploymentName string = modelName
@minValue(1)
param modelCapacity int = 1
@allowed(['DataZoneStandard', 'GlobalStandard'])
param modelSku string = 'DataZoneStandard'
param modelVersion string = defaultModelVersion(modelName)

@allowed([false])
@description('Private networking is not implemented in this public-endpoint baseline. True is refused.')
param enablePrivateNetworking bool = false

@secure()
@minLength(16)
@description('No default. Supply through a secure prompt or ARM Key Vault parameter reference. Retain for subsequent deployments.')
param postgresAdministratorPassword string

@description('Immutable image reference from the root Dockerfile. Empty provisions the foundation only.')
param containerImage string = ''
@description('Enable only after the manual init job succeeds. Workers require one always-on replica.')
param deployServices bool = false
@description('Disable to deploy the existing backend before Azure OpenAI quota is available.')
param deployAi bool = true
@allowed(['azure_blob', 'external'])
@description('azure_blob configures MCP and Blob roles. external leaves artifact configuration and permissions to the operator.')
param chartStorage string = 'azure_blob'
@description('Enable for production/tenant-sealed installs. Cannot be disabled again on an existing vault.')
param vaultPurgeProtection bool = false
@allowed(['discord', 'teams', 'mcp', 'scheduler'])
@description('Serving apps to create. Removing an entry does not stop an existing app.')
param services array = ['discord', 'teams', 'mcp', 'scheduler']

@description('Optional nonsecret environment variables, common to all services. Put credentials in secretNames instead.')
param appEnvironment object = {}
@description('Additional environment variable -> Key Vault secret-name mappings, common to all services. Values are names, never secret values.')
param secretNames object = {}
@description('Per-service nonsecret environment variables, keyed by discord/teams/mcp/scheduler/init.')
param serviceEnvironment object = {}
@description('Per-service environment variable -> Key Vault secret-name mappings.')
param serviceSecretNames object = {}
@description('Optional additional Postgres firewall ranges: [{name, startIpAddress, endIpAddress}]. Azure-service access is enabled for ACA.')
param postgresFirewallRules array = []

var checkedNamePrefix = validateNamePrefix(namePrefix)
var checkedChartStorage = validateChartStorage(chartStorage, secretNames, serviceSecretNames.?mcp ?? {})
var suffix = uniqueString(resourceGroup().id)
var stem = '${checkedNamePrefix}-${suffix}'
var tags = { application: 'daimon' }

module foundation './modules/foundation.bicep' = {
  name: 'foundation'
  params: {
    location: location
    stem: stem
    compactName: '${checkedNamePrefix}${suffix}'
    tags: tags
    vaultPurgeProtection: vaultPurgeProtection
    chartStorage: checkedChartStorage
  }
}

module database './modules/database.bicep' = {
  name: 'database'
  params: {
    location: location
    name: '${stem}-pg'
    tags: tags
    administratorPassword: postgresAdministratorPassword
    vaultName: foundation.outputs.vaultName
    firewallRules: postgresFirewallRules
  }
}

module ai './modules/ai.bicep' = if (deployAi) {
  name: 'ai'
  params: {
    location: location
    name: '${stem}-ai'
    tags: tags
    runtimePrincipalId: foundation.outputs.identityPrincipalId
    modelName: modelName
    modelDeploymentName: modelDeploymentName
    modelCapacity: modelCapacity
    modelSku: modelSku
    modelVersion: modelVersion
  }
}

var commonEnvironment = union(
  {
    DAIMON_MCP__PUBLIC_URL: 'https://${stem}-mcp.${foundation.outputs.defaultDomain}/mcp'
    // Four processes on a burstable database: bound their combined connection budget.
    DAIMON_DATABASE__POOL_SIZE: '3'
    DAIMON_DATABASE__MAX_OVERFLOW: '2'
    PGSSLROOTCERT: '/etc/ssl/certs/ca-certificates.crt'
    AZURE_CLIENT_ID: foundation.outputs.identityClientId
  },
  appEnvironment
)
var commonSecrets = union(
  {
    DAIMON_DATABASE__URL: 'database-url'
    DAIMON_MCP__JWT_SECRET: 'mcp-jwt-secret'
    DAIMON_CRYPTO__KEYS: 'crypto-keys'
  },
  secretNames
)

var artifactsEnvironment = chartArtifactsEnvironment(
  checkedChartStorage,
  foundation.outputs.blobEndpoint,
  foundation.outputs.chartsContainerName,
  foundation.outputs.identityClientId
)

module init './modules/init.bicep' = if (!empty(containerImage)) {
  name: 'init'
  params: {
    location: location
    name: '${stem}-init'
    tags: tags
    environmentId: foundation.outputs.environmentId
    identityId: foundation.outputs.identityId
    registryServer: foundation.outputs.registryServer
    vaultUri: foundation.outputs.vaultUri
    image: containerImage
    environment: union(commonEnvironment, serviceEnvironment.?init ?? {})
    secretNames: union(commonSecrets, serviceSecretNames.?init ?? {})
  }
  dependsOn: [database]
}

// MCP's channel tools need the same platform credentials as the selected gateways.
var discordSecrets = {
  DAIMON_DISCORD__BOT_TOKEN: serviceSecretNames.?discord.?DAIMON_DISCORD__BOT_TOKEN ?? 'discord-bot-token'
}
var teamsSecrets = {
  DAIMON_TEAMS__CLIENT_SECRET: serviceSecretNames.?teams.?DAIMON_TEAMS__CLIENT_SECRET ?? 'teams-client-secret'
}
var defaultServiceSecrets = {
  discord: discordSecrets
  teams: teamsSecrets
  mcp: union(contains(services, 'discord') ? discordSecrets : {}, contains(services, 'teams') ? teamsSecrets : {})
}

var serviceDefinitions = [
  { name: 'discord', command: ['python', '-m', 'daimon.adapters.discord'], port: 0 }
  { name: 'teams', command: ['python', '-m', 'daimon.adapters.teams'], port: 3978 }
  {
    name: 'mcp'
    command: [
      'uvicorn'
      'daimon.adapters.mcp.server:create_mcp_app'
      '--factory'
      '--host'
      '0.0.0.0'
      '--port'
      '8765'
    ]
    port: 8765
  }
  { name: 'scheduler', command: ['daimon-scheduler'], port: 0 }
]
module apps './modules/app.bicep' = [
  for service in serviceDefinitions: if (deployServices && !empty(containerImage) && contains(services, service.name)) {
    name: service.name
    params: {
      location: location
      name: '${stem}-${service.name}'
      serviceName: service.name
      tags: tags
      environmentId: foundation.outputs.environmentId
      identityId: foundation.outputs.identityId
      registryServer: foundation.outputs.registryServer
      vaultUri: foundation.outputs.vaultUri
      image: containerImage
      command: service.command
      port: service.port
      environment: union(
        service.name == 'mcp' ? artifactsEnvironment : {},
        commonEnvironment,
        service.name == 'mcp' && contains(services, 'teams') ? (serviceEnvironment.?teams ?? {}) : {},
        serviceEnvironment[?service.name] ?? {}
      )
      secretNames: union(
        commonSecrets,
        defaultServiceSecrets[?service.name] ?? {},
        serviceSecretNames[?service.name] ?? {}
      )
    }
    dependsOn: [database]
  }
]

output resourceGroupName string = resourceGroup().name
output registryName string = foundation.outputs.registryName
output registryServer string = foundation.outputs.registryServer
output keyVaultName string = foundation.outputs.vaultName
output managedIdentityId string = foundation.outputs.identityId
output initJobName string = '${stem}-init'
output containerAppNames array = [for service in services: '${stem}-${service}']
output postgresHost string = database.outputs.host
output chartsBlobEndpoint string = foundation.outputs.blobEndpoint
output chartsContainerName string = foundation.outputs.chartsContainerName
output mcpArtifactsEnvironment object = artifactsEnvironment
output foundryProjectId string = deployAi ? ai!.outputs.projectId : ''
output daimonEnvironment object = commonEnvironment
output daimonSecretNames object = commonSecrets
output harnessEnvironment object = {
  AZURE_CLIENT_ID: foundation.outputs.identityClientId
  AZURE_OPENAI_ENDPOINT: deployAi ? ai!.outputs.openaiEndpoint : ''
  AZURE_OPENAI_DEPLOYMENT: deployAi ? modelDeploymentName : ''
  AZURE_OPENAI_API_VERSION: deployAi ? '2025-04-01-preview' : ''
  AZURE_AI_PROJECT_ENDPOINT: deployAi ? ai!.outputs.projectEndpoint : ''
}
output privateNetworkingEnabled bool = enablePrivateNetworking

import { validateNamePrefix } from '../names.bicep'
import { validateChartStorage } from '../chart-storage.bicep'

targetScope = 'subscription'
param environmentName string
param location string
@minLength(3)
@maxLength(8)
param namePrefix string = 'daimon'
@secure()
param postgresAdministratorPassword string
param containerImage string = ''
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
param modelName string = 'gpt-6.1-sol'
param modelDeploymentName string = modelName
@minValue(1)
param modelCapacity int = 1
@allowed(['DataZoneStandard', 'GlobalStandard'])
param modelSku string = 'DataZoneStandard'
param modelVersion string = '2026-09-29'
@allowed([false])
param enablePrivateNetworking bool = false
param appEnvironment object = {}
param serviceEnvironment object = {}
param secretNames object = {}
param serviceSecretNames object = {}
param postgresFirewallRules array = []

var checkedNamePrefix = validateNamePrefix(namePrefix)
var checkedChartStorage = validateChartStorage(chartStorage, secretNames, serviceSecretNames.?mcp ?? {})

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${checkedNamePrefix}-${environmentName}'
  location: location
  tags: { 'azd-env-name': environmentName }
}
module stack '../main.bicep' = {
  name: 'daimon'
  scope: group
  params: {
    location: location
    namePrefix: checkedNamePrefix
    postgresAdministratorPassword: postgresAdministratorPassword
    containerImage: containerImage
    deployServices: deployServices
    deployAi: deployAi
    chartStorage: checkedChartStorage
    vaultPurgeProtection: vaultPurgeProtection
    services: services
    modelName: modelName
    modelDeploymentName: modelDeploymentName
    modelCapacity: modelCapacity
    modelSku: modelSku
    modelVersion: modelVersion
    enablePrivateNetworking: enablePrivateNetworking
    appEnvironment: appEnvironment
    serviceEnvironment: serviceEnvironment
    secretNames: secretNames
    serviceSecretNames: serviceSecretNames
    postgresFirewallRules: postgresFirewallRules
  }
}
output AZURE_RESOURCE_GROUP string = group.name
output AZURE_CONTAINER_REGISTRY_NAME string = stack.outputs.registryName
output AZURE_KEY_VAULT_NAME string = stack.outputs.keyVaultName
output AZURE_INIT_JOB_NAME string = stack.outputs.initJobName
output AZURE_AI_PROJECT_ENDPOINT string = deployAi ? stack.outputs.harnessEnvironment.AZURE_AI_PROJECT_ENDPOINT : ''
output AZURE_OPENAI_ENDPOINT string = deployAi ? stack.outputs.harnessEnvironment.AZURE_OPENAI_ENDPOINT : ''
output AZURE_OPENAI_DEPLOYMENT string = deployAi ? stack.outputs.harnessEnvironment.AZURE_OPENAI_DEPLOYMENT : ''
output AZURE_CLIENT_ID string = stack.outputs.harnessEnvironment.AZURE_CLIENT_ID
output DAIMON_MCP_ARTIFACTS_ENVIRONMENT object = stack.outputs.mcpArtifactsEnvironment

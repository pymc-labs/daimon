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
param gpt5DeploymentName string = 'gpt-5'
param gpt5Capacity int = 1
param gpt5Sku string = 'GlobalStandard'
param gpt5ModelVersion string = '2025-08-07'
@allowed([false])
param enablePrivateNetworking bool = false
param appEnvironment object = {}
param serviceEnvironment object = {}
param secretNames object = {}
param serviceSecretNames object = {
  discord: { DAIMON_DISCORD__BOT_TOKEN: 'discord-bot-token' }
  teams: { DAIMON_TEAMS__CLIENT_SECRET: 'teams-client-secret' }
}
param postgresFirewallRules array = []

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${namePrefix}-${environmentName}'
  location: location
  tags: { 'azd-env-name': environmentName }
}
module stack '../main.bicep' = {
  name: 'daimon'
  scope: group
  params: {
    location: location
    namePrefix: namePrefix
    postgresAdministratorPassword: postgresAdministratorPassword
    containerImage: containerImage
    deployServices: deployServices
    gpt5DeploymentName: gpt5DeploymentName
    gpt5Capacity: gpt5Capacity
    gpt5Sku: gpt5Sku
    gpt5ModelVersion: gpt5ModelVersion
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
output AZURE_AI_PROJECT_ENDPOINT string = stack.outputs.harnessEnvironment.AZURE_AI_PROJECT_ENDPOINT
output AZURE_OPENAI_ENDPOINT string = stack.outputs.harnessEnvironment.AZURE_OPENAI_ENDPOINT
output AZURE_OPENAI_DEPLOYMENT string = stack.outputs.harnessEnvironment.AZURE_OPENAI_DEPLOYMENT
output AZURE_CLIENT_ID string = stack.outputs.harnessEnvironment.AZURE_CLIENT_ID

param location string
param stem string
@minLength(5)
@maxLength(23)
param compactName string
param tags object
param vaultPurgeProtection bool
@allowed(['azure_blob', 'external'])
param chartStorage string = 'azure_blob'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${stem}-runtime'
  location: location
  tags: tags
}
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${stem}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 }
  }
}
resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${stem}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${compactName}acr'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${compactName}v'
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: vaultPurgeProtection
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Enabled'
    accessPolicies: []
  }
}
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: '${compactName}s'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Enabled'
  }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: { enabled: true, days: 7 }
    containerDeleteRetentionPolicy: { enabled: true, days: 7 }
  }
}
resource charts 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'charts'
  properties: { publicAccess: 'None' }
}
resource pullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, 'acr-pull')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}
resource secretsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, 'secrets-user')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}
// Container scope only; no storage-account management or key access.
resource chartsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (chartStorage == 'azure_blob') {
  scope: charts
  name: guid(charts.id, identity.id, 'blob-contributor')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
    )
  }
}
// Delegation keys require account scope; blob data access stays container-scoped.
resource chartsDelegatorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (chartStorage == 'azure_blob') {
  scope: storage
  name: guid(storage.id, identity.id, 'blob-delegator')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'
    )
  }
}
output environmentId string = environment.id
output defaultDomain string = environment.properties.defaultDomain
output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
output blobEndpoint string = storage.properties.primaryEndpoints.blob
output chartsContainerName string = charts.name

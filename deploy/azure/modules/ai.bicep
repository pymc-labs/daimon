param location string
param name string
param tags object
param runtimePrincipalId string
param deploymentName string
param capacity int
param deploymentSku string
param modelVersion string

// AIServices supports both Azure OpenAI deployments and Foundry child projects.
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: name
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: name
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}
resource model 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: deploymentName
  sku: { name: deploymentSku, capacity: capacity }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5', version: modelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: 'daimon'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: { displayName: 'Daimon', description: 'Daimon hosted-agent project' }
}
var openaiUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)
resource runtimeInference 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, runtimePrincipalId, openaiUserRole)
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: openaiUserRole
  }
}
resource projectInference 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, project.id, openaiUserRole)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: openaiUserRole
  }
}
output openaiEndpoint string = 'https://${account.name}.openai.azure.com/'
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output projectId string = project.id

param location string
param name string
param tags object
param environmentId string
param identityId string
param registryServer string
param vaultUri string
param image string
param environment object
param secretNames object

var plainEnvironment = [for item in items(environment): { name: item.key, value: string(item.value) }]
var secretEnvironment = [
  for item in items(secretNames): {
    name: item.key
    secretRef: toLower(replace(item.key, '_', '-'))
  }
]

resource init 'Microsoft.App/jobs@2025-01-01' = {
  name: name
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registryServer, identity: identityId }]
      secrets: [
        for item in items(secretNames): {
          name: toLower(replace(item.key, '_', '-'))
          keyVaultUrl: '${vaultUri}secrets/${item.value}'
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'init'
          image: image
          command: ['sh', '-c', 'alembic upgrade head && daimon defaults apply']
          env: concat(plainEnvironment, secretEnvironment)
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
    }
  }
}

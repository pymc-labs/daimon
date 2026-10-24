param location string
param name string
param serviceName string
param tags object
param environmentId string
param identityId string
param registryServer string
param vaultUri string
param image string
param command array
param port int
param environment object
param secretNames object

var plainEnvironment = [for item in items(environment): { name: item.key, value: string(item.value) }]
var secretEnvironment = [
  for item in items(secretNames): {
    name: item.key
    secretRef: toLower(replace(item.key, '_', '-'))
  }
]

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  tags: union(tags, { 'azd-service-name': serviceName })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: port > 0
        ? {
            external: true
            targetPort: port
            transport: 'http'
            allowInsecure: false
          }
        : null
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
      terminationGracePeriodSeconds: 90
      containers: [
        {
          name: serviceName
          image: image
          command: command
          env: concat(plainEnvironment, secretEnvironment)
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: serviceName == 'mcp'
            ? [
                {
                  type: 'Readiness'
                  httpGet: { path: '/healthz', port: port }
                  initialDelaySeconds: 10
                  periodSeconds: 10
                }
              ]
            : contains(['discord', 'scheduler'], serviceName)
                ? [
                    {
                      type: 'Liveness'
                      httpGet: { path: '/healthz', port: serviceName == 'discord' ? 8081 : 8082 }
                      initialDelaySeconds: 60
                      periodSeconds: 10
                      timeoutSeconds: 5
                      failureThreshold: 3
                    }
                  ]
                : []
        }
      ]
      // Gateway and scheduler processes cannot wake on HTTP. Keep one replica each.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

param location string
param name string
param tags object
@secure()
param administratorPassword string
param vaultName string
param firewallRules array

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: name
  location: location
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '17'
    administratorLogin: 'daimon'
    administratorLoginPassword: administratorPassword
    storage: { storageSizeGB: 32, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}
resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: 'daimon'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}
// This Azure-wide firewall exception is a public-baseline compromise, not tenant isolation.
resource azureAccess 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: server
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}
resource operatorAccess 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = [
  for rule in firewallRules: {
    parent: server
    name: rule.name
    properties: { startIpAddress: rule.startIpAddress, endIpAddress: rule.endIpAddress }
  }
]
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: vaultName }
resource databaseUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'database-url'
  properties: {
    // asyncpg takes ssl=verify-full (libpq's sslmode is not an asyncpg keyword).
    value: 'postgresql+asyncpg://daimon:${uriComponent(administratorPassword)}@${server.properties.fullyQualifiedDomainName}:5432/daimon?ssl=verify-full'
  }
  dependsOn: [database]
}
output host string = server.properties.fullyQualifiedDomainName

// Azure Bot for the Teams adapter, with an optional Microsoft Teams channel.
// Creates no credential: the app identity and its secret are made separately.
targetScope = 'resourceGroup'

@minLength(2)
@maxLength(64)
@description('Globally unique Azure Bot resource name.')
param name string
@description('Name shown for the bot in the Azure portal. People in Teams see the app package name.')
param displayName string = 'daimon'
@description('Entra application (client) ID the Teams adapter authenticates as.')
param clientId string
@description('Directory (tenant) ID of that single-tenant application.')
param tenantId string
@description('Public HTTPS base URL of the Teams adapter, without a trailing slash or path.')
@minLength(9)
param teamsBaseUrl string
@allowed(['F0', 'S1'])
param sku string = 'F0'
@description('Set true only after the tenant owner has accepted the Microsoft Teams channel terms. False leaves any existing channel untouched.')
param teamsChannelTermsAccepted bool = false
param tags object = {}

var endpoint = startsWith(teamsBaseUrl, 'https://') && !endsWith(teamsBaseUrl, '/')
  ? '${teamsBaseUrl}/api/messages'
  : fail('teamsBaseUrl must use HTTPS and have no trailing slash')

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: name
  location: 'global'
  kind: 'azurebot'
  sku: { name: sku }
  tags: tags
  properties: {
    displayName: displayName
    endpoint: endpoint
    msaAppId: clientId
    msaAppTenantId: tenantId
    msaAppType: 'SingleTenant'
    publicNetworkAccess: 'Enabled'
    isStreamingSupported: false
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = if (teamsChannelTermsAccepted) {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  kind: 'azurebot'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
      acceptedTerms: teamsChannelTermsAccepted
      deploymentEnvironment: 'CommercialDeployment'
      enableCalling: false
    }
  }
}

output messagingEndpoint string = endpoint

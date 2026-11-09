// Entra application and service principal for the Teams adapter's bot.
// Deploy once, before the main stack, by someone allowed to create applications.
// Requests no API permissions, redirects or implicit grants, and creates no secret.
extension 'br:mcr.microsoft.com/bicep/extensions/microsoftgraph/v1.0:1.0.0'

@description('Immutable Graph alternate key for the application. Reuse it on every deployment of the same bot.')
@minLength(1)
param uniqueName string
@description('Application name shown in Entra.')
param displayName string = 'daimon'

resource application 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: uniqueName
  displayName: displayName
  signInAudience: 'AzureADMyOrg'
  isFallbackPublicClient: false
  publicClient: { redirectUris: [] }
  web: {
    redirectUris: []
    implicitGrantSettings: { enableIdTokenIssuance: false, enableAccessTokenIssuance: false }
  }
  spa: { redirectUris: [] }
  identifierUris: []
  api: { requestedAccessTokenVersion: 2, oauth2PermissionScopes: [], preAuthorizedApplications: [] }
  appRoles: []
  requiredResourceAccess: []
}

resource servicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: application.appId
  accountEnabled: true
  appRoleAssignmentRequired: false
}

output clientId string = application.appId
output tenantId string = tenant().tenantId

@export()
func isValidChartStorage(value string, appSecretNames object, mcpSecretNames object) bool =>
  value == 'external' || (value == 'azure_blob' && !contains(appSecretNames, 'DAIMON_ARTIFACTS__ACCESS_KEY_ID') && !contains(appSecretNames, 'DAIMON_ARTIFACTS__SECRET_ACCESS_KEY') && !contains(mcpSecretNames, 'DAIMON_ARTIFACTS__ACCESS_KEY_ID') && !contains(mcpSecretNames, 'DAIMON_ARTIFACTS__SECRET_ACCESS_KEY'))

@export()
func validateChartStorage(value string, appSecretNames object, mcpSecretNames object) 'azure_blob' | 'external' =>
  isValidChartStorage(value, appSecretNames, mcpSecretNames)
    ? (value == 'external' ? 'external' : 'azure_blob')
    : fail('S3 artifact secret mappings are incompatible with chartStorage=azure_blob; set chartStorage=\'external\'.')

@export()
func chartArtifactsEnvironment(value string, endpoint string, bucket string, clientId string) object =>
  value == 'azure_blob' ? {
    DAIMON_ARTIFACTS__BACKEND: 'azure_blob'
    DAIMON_ARTIFACTS__ENDPOINT_URL: endpoint
    DAIMON_ARTIFACTS__BUCKET: bucket
    DAIMON_ARTIFACTS__AZURE_CLIENT_ID: clientId
  } : {}

// Pure lookup so version defaults can also be verified by build-params offline.
@export()
func knownModelVersion(modelName string) string =>
  modelName == 'gpt-6.1-sol' ? '2026-09-29'
    : modelName == 'gpt-6-luna' ? '2026-09-22'
    : ''

// Other models must supply modelVersion explicitly.
@export()
func defaultModelVersion(modelName string) string =>
  empty(knownModelVersion(modelName))
    ? fail('No default version for modelName; supply modelVersion explicitly')
    : knownModelVersion(modelName)

// Known model ids are reserved aliases: they must identify their own model.
@export()
func isValidModelDeploymentName(modelName string, deploymentName string) bool =>
  empty(knownModelVersion(deploymentName)) || deploymentName == modelName

@export()
func validateModelDeploymentName(modelName string, deploymentName string) string =>
  isValidModelDeploymentName(modelName, deploymentName)
    ? deploymentName
    : fail('modelDeploymentName is a reserved model name and must equal modelName; choose a custom alias instead')

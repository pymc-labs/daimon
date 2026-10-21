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

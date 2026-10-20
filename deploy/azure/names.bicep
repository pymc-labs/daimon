// Keep the predicate pure so parameter examples can exercise it offline.
@export()
func isValidNamePrefix(value string) bool =>
  length(value) >= 3 && length(value) <= 8 && contains('abcdefghijklmnopqrstuvwxyz', take(value, 1)) && empty(filter(
    range(0, length(value)),
    i => !contains('abcdefghijklmnopqrstuvwxyz0123456789', substring(value, i, 1))
  ))

// Validate before constructing any storage, registry, vault or resource-group names.
@export()
func validateNamePrefix(value string) string =>
  isValidNamePrefix(value)
    ? value
    : fail('namePrefix must be 3-8 lowercase alphanumeric characters starting with a lowercase letter')

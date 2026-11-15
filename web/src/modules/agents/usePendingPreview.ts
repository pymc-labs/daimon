import { modelValue, useModelPreview } from "./api"
import type { Agent, ModelChoice } from "./types"

/** The preview of a draft's model pick, or none while it is the model the agent runs. */
export function usePendingPreview(agent: Agent, model: ModelChoice) {
  const moved = model.backend !== agent.model.backend || model.id !== agent.model.id
  return useModelPreview(agent.id, modelValue(model), moved)
}

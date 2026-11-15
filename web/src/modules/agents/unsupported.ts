import type { ModelPreview } from "./types"

/** What the picked model's provider can't run, from the preview: skill ids, server keys, capabilities and drops. */
export type Unsupported = { skills: Set<string>; servers: Set<string>; capabilities: Set<string> }

export const NOTHING: Unsupported = { skills: new Set(), servers: new Set(), capabilities: new Set() }

/** The lists of a preview as sets; a drop (memory, sandbox) counts as a lost capability. Pure. */
export function unsupportedOf(preview?: ModelPreview | null): Unsupported {
  if (!preview?.moves || preview.refusal) return NOTHING
  return { skills: new Set(preview.skills), servers: new Set(preview.servers), capabilities: new Set([...preview.capabilities, ...preview.drops]) }
}

import type { ArtifactAccess, DataAccess } from "./types"

export type Uses = { data: DataAccess; events: boolean; ask: boolean }

const WRITE = "A public page can't write data"
const BOTH = "A public page can't read data and send events"
const ASK = "A public page can't ask the agent"

/** Why each setting is refused while the page is public, or null when it is allowed. */
export function refused(access: ArtifactAccess, u: Uses) {
  const pub = access === "public"
  return {
    write: pub ? WRITE : null,
    read: pub && u.events ? BOTH : null,
    events: pub && u.data !== "off" ? BOTH : null,
    ask: pub ? ASK : null,
  }
}

/** Why a page with these settings can't be made public, or null. */
export function publicRefusal(u: Uses): string | null {
  if (u.data === "write") return WRITE
  if (u.ask) return ASK
  if (u.data === "read" && u.events) return BOTH
  return null
}

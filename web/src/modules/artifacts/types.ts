export type ArtifactAccess = "workspace" | "people" | "public"
export type DataAccess = "off" | "read" | "write"
export type ArtifactRole = "owner" | "editor" | "admin" | "viewer"
export type PersonRef = { id: string; name: string; kind: "member" | "visitor" }
/** Which documents a page's viewers may read and change in one collection. */
export type CollectionRule = { read: "all" | "own"; write: "all" | "own" | "none" }
export type ArtifactSummary = {
  id: string; slug: string; title: string; url: string   // url: /a/<id>/<slug>
  agent: { id: string; name: string; avatar?: string | null }
  owner: PersonRef; mine: boolean; role: ArtifactRole
  access: ArtifactAccess; people_count: number
  data: DataAccess; events: boolean; ask: boolean
  version: number; size_bytes: number; updated_at: string; created_at: string; expires_at: string | null
}
export type ArtifactDetail = ArtifactSummary & {
  people: PersonRef[]; editors: PersonRef[]; editors_can_share: boolean; data_scale: number
  collection_rules: Record<string, CollectionRule>
  link: string | null   // public: full secret link
  usage: { documents: number; max_documents: number; bytes: number; max_bytes: number }
  watches: ArtifactWatch[]
  asks: { threads: number; turns_7d: number }
  can: { edit: boolean; share: boolean; delete: boolean; transfer: boolean }
}
export type ArtifactVersion = { version: number; created_at: string; size_bytes: number; by: string; agent: string; current: boolean; chat_url: string | null }
export type DataCollection = { name: string; documents: number; bytes: number }
export type DataDocument = { id: string; data: unknown; updated_at: string; updated_by: string; size_bytes: number }
export type ArtifactEvent = { id: string; event: string; data: unknown; by: string | null; sender: "member" | "visitor" | "anonymous"; at: string; delivered: number }

/** What `PATCH /api/artifacts/:id` takes; people and editors are ids. */
export type ArtifactChange = Partial<{
  title: string; access: ArtifactAccess; people: string[]; editors: string[]; editors_can_share: boolean
  data: DataAccess; events: boolean; ask: boolean; data_scale: number; expires_at: string | null
  collection_rules: Record<string, CollectionRule>
}>
/** What `POST /api/artifacts/:id/people` answers: a new visitor's sign-in link, shown once. */
export type EmailShare = ArtifactDetail & { invite_link: string | null }

/** A watch on a page; `mine` when the caller made it, the only one who may resume it. */
export type ArtifactWatch = { id: string; name: string; owner: string; mine: boolean; agent: string; status: "active" | "paused"; turns_today: number; max_turns_per_day: number; where?: string | null; thread_url: string | null }

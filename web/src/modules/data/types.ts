// What `/api/data`, `/api/integrations/data`, `/api/admin/data` and `/api/profile/data` answer.

export type DataSource = { id: string; name: string; kind: string; status: string; records: number; last_sync_at: string | null; fresh: boolean }
export type DataItem = { id: string; source_id: string; kind: string; title: string; url: string | null; author: string | null; occurred_at: string; scope: string }
export type Hit = { record_id: string; source_id: string; kind: string; title: string; url: string | null; occurred_at: string; scope: string; snippet: string }
export type LiveHit = { title: string; kind: string; url: string | null; text: string }
export type Live = { source_id: string; source_name: string; hits: LiveHit[]; skipped: string | null }
export type Search = { hits: Hit[]; live: Live[]; notes: string[] }
export type DataFile = { ord: number; name: string; content_type: string; size: number }
export type DataRecord = { item: DataItem; text: string; next_start: number | null; files: DataFile[] }

export type KindField = { name: string; label: string; kind: "text" | "secret" | "select" | "list"; options: string[]; required: boolean; placeholder: string }
export type Kind = { kind: string; name: string; category: string; live: boolean; fields: KindField[] }
export type Parity = { finished_at: string | null; people: number; checked: number; wrong_more: number; wrong_less: number; unresolved: string | null; ok: boolean }
export type SourceState = {
  id: string; name: string; kind: string; mode: "stored" | "federated"; status: "active" | "paused" | "error" | "disabled"
  publication: "shadow" | "published" | "retired"; bound: boolean; default_scope: string; created_at: string; records: number
  last_sync_at: string | null; last_status: string | null; last_error: string | null; errors: number
  parity: Parity | null; can_publish: boolean; blocked: string | null
}
export type Run = { id: string; trigger: string; status: string; started_at: string; finished_at: string | null; added: number; updated: number; deleted: number; error: string | null }
export type SourceAction = "pause" | "resume" | "retire" | "publish" | "unpublish" | "sync"
export type NewSource = { kind: string; name: string; settings: Record<string, string | string[]> }

export type Grant = { holder: string; name: string | null; bound: boolean }
export type Scope = { id: string; kind: string; name: string; description: string; bound: boolean; holders: Grant[] }
export type SourceIdentity = {
  provider: string; workspace: string; subject: string; name: string; email: string | null; email_verified: boolean
  account_id: string | null; account_name: string | null; proof: string | null; linked_at: string | null; redacted: boolean
}
export type Candidate = { provider: string; workspace: string; subject: string; name: string; email: string; account_id: string; account_name: string | null }
export type Admins = { admins: { account_id: string; name: string | null; created_at: string }[]; you: boolean; can_grant: boolean }
export type RedactionSet = {
  name: string; repo: string | null; path: string | null; ref: string | null; approved_sha: string | null; pin_sha: string | null
  pending: boolean; usable: boolean; checked_at: string | null; error: string | null
}
export type Rules = {
  aliases: Record<string, string>; removals: string[]; regex: { pattern: string; replace: string }[]
  exclude_ids: string[]; entities: { name: string; aliases: string[]; scope: string | null; label: string }[]
}
export type RedactionDetail = { info: RedactionSet; rules: Rules | null; pending_rules: Rules | null }

export type Account = { provider: string; workspace: string; name: string; email: string | null; proof: string | null; linked_at: string | null }
export type Connection = { source_id: string; source: string; provider: string; subject: string; status: string; last_ok_at: string | null }
export type Connectable = { source_id: string; name: string; provider: string }
export type PersonalPart = "drive" | "calendar"
export type Notice = { kind: "linked" | "unlinked"; identity: string; provider: string; name: string }
export type Connected = { accounts: Account[]; notices: Notice[]; connections: Connection[]; connectable: Connectable[]; personal: Partial<Record<PersonalPart, boolean>> }
export type Started = { flow: "code" | "device"; url: string; pending_id: string; user_code: string; interval: number; source: string; expires_at: string | null }

import type { AgentRef, Identity, Role } from "@/lib/types"

export type AdminPerson = {
  id: string
  name: string
  email: string
  role: Role
  status: "active" | "suspended"
  groups: string[]
  identities: Identity[]
  credit: { left_usd: number; total_usd: number } | null
  last_seen: string | null
  /** A visitor with a live invite: the invite and whether it lets them attach files. */
  invite_id?: string | null
  allow_files?: boolean | null
}

export type Invite = {
  id: string
  email: string
  role: "visitor" | "member"
  agents: AgentRef[]
  credit_usd: number
  allow_files: boolean
  expires_at: string
  status: "pending" | "accepted" | "expired"
  by: string
  link?: string
}

export type AgentAccess = {
  agent: AgentRef
  web: boolean
  who: "everyone" | "groups" | "invites"
  groups: string[]
  fixed_to: string | null
  fixed_name: string | null
}

export type AuditSource = "web" | "security"
export type AuditTarget = { kind: "person" | "invite" | "other"; id: string | null; name: string }
export type AuditEntry = {
  id: string; source: AuditSource; at: string; who: string; actor_id: string | null; action: string; target: AuditTarget; detail?: string
  /** Security events only: every readable field the event stores. */
  facts?: [string, string][]
  /** Security events only: where it happened; `name` is null while the channel is unnamed. */
  channel?: { id: string; name: string | null } | null
  /** Security events only: the message and turn ids, to copy; never shown. */
  reference?: string | null
}
export type AuditKind = { source: AuditSource; value: string; label: string }

export type Access = {
  /** The people who may start turns; null when everyone may. */
  people: { id: string; name: string }[] | null
  /** Allowed ids of people the web app doesn't list. */
  others: number
}

export type Providers = {
  offered: { id: string; label: string }[]
  allowed: string[]
  /** Whether the list follows the deployment's keyed providers. */
  followed: boolean
}

export type WorkspaceSettings = {
  default_agent: string
  access: Access
  /** Null where choosing providers is switched off. */
  providers: Providers | null
}

export type SettingsPatch = {
  default_agent?: string
  invokers?: { everyone: boolean; people: string[] }
  providers?: string[] | "default"
}

export type FeedbackReason = { code: string; label: string; count: number | null }
export type AgentFeedback = { id: string | null; name: string; up: number | null; down: number | null; reasons: FeedbackReason[] }
export type Feedback = { days: number; agents: AgentFeedback[] }

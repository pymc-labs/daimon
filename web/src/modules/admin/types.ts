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
}

export type Invite = {
  id: string
  email: string
  role: "visitor" | "member"
  agents: AgentRef[]
  credit_usd: number
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
export type AuditEntry = {
  id: string; source: AuditSource; at: string; who: string; action: string; target: string; detail?: string
  /** Security events only: every readable field the event stores. */
  facts?: [string, string][]
}
export type AuditKind = { source: AuditSource; value: string; label: string }

export type Access = {
  /** The people who may start turns; null when everyone may. */
  people: { id: string; name: string }[] | null
  /** Allowed ids of people the web app doesn't list. */
  others: number
  /** Null where the workspace's chat platform has no such setting. */
  dm_enabled: boolean | null
  dm_memory_read_only: boolean | null
  plain_text_mentions: boolean | null
}

export type Providers = {
  offered: { id: string; label: string }[]
  allowed: string[]
  /** Whether the list follows the deployment's keyed providers. */
  followed: boolean
}

export type WorkspaceSettings = {
  modules: Record<"chat" | "agents" | "channels" | "routines" | "credits" | "integrations", boolean>
  default_agent: string
  access: Access
  /** Null where choosing providers is switched off. */
  providers: Providers | null
}

export type SettingsPatch = {
  modules?: WorkspaceSettings["modules"]
  default_agent?: string
  invokers?: { everyone: boolean; people: string[] }
  dm_enabled?: boolean
  dm_memory_read_only?: boolean
  plain_text_mentions?: boolean
  providers?: string[] | "default"
}

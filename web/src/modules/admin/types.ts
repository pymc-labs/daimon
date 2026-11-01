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

export type AuditEntry = { id: string; at: string; who: string; action: string; target: string; detail?: string }

export type WorkspaceSettings = {
  modules: Record<"chat" | "agents" | "credits", boolean>
  default_agent: string
}

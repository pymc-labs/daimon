// Shared API contract. Module-specific shapes live in `modules/<key>/types.ts`.

export type Role = "owner" | "admin" | "member" | "visitor"
export type Platform = "discord" | "slack" | "teams" | "web"
export type ModuleKey = "chat" | "agents" | "credits" | "admin"
export type Capability =
  | "chat.use" | "agents.view" | "agents.edit" | "agents.create"
  | "credits.view_own" | "credits.manage" | "people.manage" | "audit.view" | "workspace.manage"

export type Workspace = { id: string; name: string; platform: Platform; role: Role; members: number }

/** An app embedded through the embed protocol; name and icon come from deployment config. */
export type EmbeddedApp = {
  key: string
  name: string
  icon: string
  origin: string
  nav: { label: string; path: string; icon: string }[]
  /** Page where the app manages the MCP connections it serves. */
  connections_path?: string
}

/** A linked sign-in: provider id and name, the account's label, and the provider's configured icon. */
export type Identity = { provider: string; provider_label: string; label: string; icon?: string }

export type CreditMeter = { left_usd: number; total_usd: number; window: "month" | "total"; resets_at: string | null }

export type Me = {
  id: string
  name: string
  email: string
  workspace: Workspace
  workspaces: Workspace[]
  role: Role
  modules: ModuleKey[]
  capabilities: Capability[]
  identities: Identity[]
  credit: CreditMeter | null
  /** Visitors: who invited them, named when credit runs out. */
  invited_by?: string
  apps: EmbeddedApp[]
}

export type AgentRef = { id: string; name: string; avatar: string; model: string; description: string; starters?: string[] }

/** A group people can belong to, for pickers; `everyone` is the built-in one. */
export type Group = { id: string; name: string; members: number }

export type ChatSession = { id: string; title: string; agent: AgentRef; updated_at: string; running?: boolean }

export type Person = {
  id: string
  name: string
  email: string
  role: Role
  title?: string
}

/** Sign-in methods the server offers; `dev` signs in locally without leaving the page. */
export type Provider = { id: string; label: string; kind: "redirect" | "dev"; icon?: string }

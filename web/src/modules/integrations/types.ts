export type InstallStatus = "connected" | "pending" | "failed" | "archived" | "disconnected"
export type Install = {
  platform: "discord" | "slack" | "teams"
  name: string
  status: InstallStatus
  problem: boolean
  teams: { id: string; name: string | null }[]
}
export type Workspaces = { workspaces: Install[] }

export type Access = "read" | "write"
export type GithubRepo = { name: string; access: Access; shared: boolean; status: "active" | "suspended" | "revoked"; working: boolean }
export type Github = {
  configured: boolean
  installations: { id: string; account: string; selection: string | null; repos: number; state: "active" | "suspended" | "removed"; refreshed_at: string | null }[]
  agents: { id: string; name: string; repos: GithubRepo[] }[]
  requests: { id: string; agent: string; agent_id: string | null; repos: string[]; access: Access; requester: string; requester_id: string | null; created_at: string; expires_at: string; waiting_github: boolean }[]
  trouble: { kind: "push" | "installation"; subject: string; attempts: number; error: string; at: string }[]
}

export type McpServer = {
  id: string
  name: string
  url: string
  agents: { id: string; name: string; shared: boolean; signed_in: number }[]
  signed_in: number
}
export type Mcp = { servers: McpServer[] }
export type McpExpiry = { complete: boolean; servers: { id: string; expired: number; people: string[] }[] }

export type Scope = { id: string; label: string }
export type ApiToken = { id: string; label: string | null; scopes: string[]; created_at: string; expires_at: string | null }
export type Tokens = { configured: boolean; scopes: Scope[]; days: number; tokens: ApiToken[] }
export type Minted = { token: string; id: string; scopes: string[]; expires_at: string }

export type ConnectionTool = {
  name: string
  description: string
  seen_description: string
  seen_arguments: string[]
  seen: string
  changed: boolean
  enabled: boolean
  writes: boolean
  scope: string | null
}
export type Connection = {
  id: string
  name: string
  url: string
  status: "active" | "paused" | "error"
  error: string | null
  refreshed_at: string | null
  scope: string | null
  declared: boolean
  has_token: boolean
  tools: ConnectionTool[]
}
export type Connections = { enabled: boolean; connections: Connection[] }

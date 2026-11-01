// Agents module contract: the shapes `/api/agents/...` returns. Mirrors the agent config in packages/core.
import type { Platform } from "@/lib/types"

/** When an applied change reaches conversations (session_snapshot identity vs mutable axes). */
export type Effect = "live" | "session" | "threads"
/** mux `Provider`. Anthropic runs on Managed Agents; the others are neutral backends. */
export type Backend = "anthropic" | "openai" | "gemini" | "selfhosted"
export type ToolRule = "allow" | "ask" | "deny"
export type Effort = "low" | "medium" | "high" | "xhigh" | "max"

export type ModelChoice = { id: string; backend: Backend; speed: "standard" | "fast"; effort: Effort }
export type CatalogModel = { id: string; label: string; backend: Backend; in_usd: number; out_usd: number; fast?: boolean }

/** `agent_toolset_20260401` configs. */
export type BuiltinTool = { name: string; enabled: boolean }

type McpTool = { name: string; description: string; effect: "read" | "write"; rule: ToolRule }
export type McpServer = {
  key: string
  name: string
  url: string
  auth: "oauth" | "token" | "none"
  /** `agent_mcp_credentials`: stored once per agent, mirrored into each caller's vault. */
  credential: "connected" | "missing" | "expired" | null
  tools: McpTool[]
}

export type Skill = {
  type: "custom" | "anthropic"
  skill_id: string
  title: string
  description: string
  version: string
  platforms?: Platform[]
  /** MCP server keys the skill calls. */
  requires?: string[]
}

export type Place = {
  id: string
  kind: "channel" | "dm" | "web"
  platform: Platform
  name: string
  /** Answers unaddressed in this channel (channel default). */
  default?: boolean
  confidential?: boolean
}

export type AgentConfig = {
  name: string
  purpose: string
  avatar: string
  model: ModelChoice
  /** The system prompt. Null when the reader may not see it. */
  system: string | null
  skills: Skill[]
  tools: BuiltinTool[]
  mcp_servers: McpServer[]
  /** Null when the reader may not see reach details. */
  places: Place[] | null
}

export type PersonRef = { id: string; name: string }

/** A connection node kind on an agent; `server` is the MCP server key it claims, null when the kind can be added. */
export type AgentNode = { kind: string; label: string; server: string | null }

export type Agent = AgentConfig & {
  id: string
  version: number
  owner: PersonRef
  co_owners: PersonRef[]
  updated_at: string
  updated_by: PersonRef
  last_used_at: string | null
  month_usd: number
  can_edit: boolean
  /** Prompt, credentials and reach detail: owners, co-owners and admins. */
  can_view_details: boolean
  nodes: AgentNode[]
}

export type AgentSummary = Pick<Agent, "id" | "name" | "purpose" | "avatar" | "version" | "owner" | "updated_at" | "updated_by" | "last_used_at" | "month_usd" | "can_edit" | "co_owners"> & {
  /** Changes in the signed-in person's draft. */
  draft_changes: number
  model_label: string
  backend: Backend
  runs_in: ("discord" | "slack" | "teams" | "dm" | "web")[]
}

export type Change = {
  /** Stable key: `system`, `model`, `skill:<id>`, `mcp:<key>`, `mcp:<key>/<tool>`, `tool:<name>`, `place:<id>`, `name`, `purpose`. */
  key: string
  node: string
  label: string
  kind: "add" | "remove" | "edit"
  effect: Effect
  before?: string
  after?: string
  /** Long text (prompt, skill files): diffed by line. */
  text?: boolean
}

export type Draft = { base_version: number; config: AgentConfig; changes: Change[]; stale: boolean }

export type AgentVersion = { version: number; at: string; by: PersonRef; note: string | null; changes: Change[] }

/** The files of a custom skill version; a null `content` is a binary or large file, kept as is. */
export type SkillFiles = { skill_id: string; version: string; files: { path: string; size: number; content: string | null }[] }

export type Template = { id: string; name: string; purpose: string; icon: string; skills: number; servers: number; model: string }

export type Catalog = { models: CatalogModel[]; skills: Skill[]; channels: Place[]; tools: { name: string; label: string }[] }

// The `data_server` connection node: `/api/agents/{id}/nodes/data_server`, passed through from its server.

export type DataServerToken = {
  id: string; name: string; prefix: string
  created_at: string; last_used_at: string | null; expires_at: string | null; revoked_at: string | null
}

export type DataServerActivity = { at: string; via: string; action: string; target: string | null; channel: string | null; requester: string | null }

export type ActsFor = "requester" | "self"

export type DataServerAgent = {
  id: string
  name: string
  title: string | null
  status: "active" | "suspended"
  role: string | null
  can_write: boolean
  acts_for: ActsFor
  personal: boolean
  redact_clients: boolean
  external_id: string | null
  created_at: string | null
  managed_by: string | null
  suspended_by: string | null
  manager: string | null
  platform: string | null
  scopes: string[]
  team_scopes: string[]
  /** A count in the list form; the detail form lists them, for its manager and admins. */
  tokens: number | DataServerToken[]
  /** Detail form, when `can_manage`. */
  activity?: DataServerActivity[]
  last_call_at: string | null
  can_manage: boolean
}

/** `agent` is the server's agent; `admin`: the server counts the person as an admin. */
export type DataServerStatus = {
  attached: boolean; reachable: boolean; problem: string | null
  agent: DataServerAgent | null; admin: boolean; open_url: string | null
}

export type DataServerAgentPatch = {
  name?: string; scopes?: string[]; acts_for?: ActsFor; status?: "active" | "suspended"; redact_clients?: boolean
  can_write?: boolean; personal?: boolean; role?: "admin" | "member"
}

export type DataServerTool = {
  connection_id: string; connection: string; name: string; scope: string | null; writes: boolean
  allow: boolean | null; whole: boolean; admin: boolean; granted_by: string | null; usable: boolean; grantable: boolean
}

export type DataServerConnection = {
  id: string; name: string; scope: string | null; allow: boolean | null; admin: boolean; granted_by: string | null; grantable: boolean
}

export type DataServerTools = { tools: DataServerTool[]; connections: DataServerConnection[] }

/** Set one tool (`tool_name`) or a whole connection (null) to added, removed, or back to its scope (`allow: null`). */
export type DataServerToolPut = { connection_id: string; tool_name?: string | null; allow: boolean | null }

/** A scope the signed-in person holds on the server: the only ones they may give. */
export type DataServerScope = { id: string; kind: string; name: string }

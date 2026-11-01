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
  /** Key of the embedded app whose gateway serves it; managed in that app. */
  app?: string
  tools: McpTool[]
}

export type Skill = {
  type: "custom" | "anthropic"
  skill_id: string
  title: string
  description: string
  content: string
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

/** An embedded app's view of the agent: its paths inside the app, already filled in by the server. */
export type AgentApp = { key: string; embed: string; page: string | null }

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
  apps: AgentApp[]
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

export type Template = { id: string; name: string; purpose: string; icon: string; skills: number; servers: number; model: string }

export type Catalog = { models: CatalogModel[]; skills: Skill[]; channels: Place[]; tools: { name: string; label: string }[] }

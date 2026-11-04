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
  /** False when the server's own tool list could not be read for this viewer. */
  listed?: boolean
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

/** An agent this one may ask for help: its id and name. */
export type Helper = { id: string; name: string }

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
  /** Null when the reader may not see details; absent in versions that predate helpers. */
  helpers?: Helper[] | null
}

export type PersonRef = { id: string; name: string }

/** A connection node kind on an agent; `server` is the MCP server key it claims, null when the kind can be added. */
export type AgentNode = { kind: string; label: string; server: string | null }

export type Agent = AgentConfig & {
  id: string
  version: number
  owner: PersonRef
  /** The owner on record; empty id when none, though `owner` shows the creator. */
  recorded_owner: PersonRef
  co_owners: PersonRef[]
  updated_at: string
  updated_by: PersonRef
  last_used_at: string | null
  spend_usd: number
  can_edit: boolean
  /** Prompt, credentials and reach detail: owners, co-owners and admins. */
  can_view_details: boolean
  nodes: AgentNode[]
}

export type AgentSummary = Pick<Agent, "id" | "name" | "purpose" | "avatar" | "version" | "owner" | "updated_at" | "updated_by" | "last_used_at" | "spend_usd" | "can_edit" | "co_owners"> & {
  /** Changes in the signed-in person's draft. */
  draft_changes: number
  model_label: string
  backend: Backend
  runs_in: ("discord" | "slack" | "teams" | "dm" | "web")[]
}

export type Change = {
  /** Stable key: `system`, `model`, `skill:<id>`, `mcp:<key>`, `mcp:<key>/<tool>`, `tool:<name>`, `place:<id>`, `helper:<id>`, `name`, `purpose`. */
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

/** A stored key as the keys node lists it: its name only, never the value. */
export type AgentKey = { key: string; updated_at: string }

/** A coding key as listed: who made it and when it ends, never the token. */
export type CodingKey = { id: string; creator: string; created_at: string; expires_at: string | null; mine: boolean }

/** A coding key as made: `token` appears here once. */
export type MintedCodingKey = { id: string; token: string; command: string; config: string; expires_at: string }

export type SkillUse = { kind: "agent" | "channel"; id: string; name: string; /** The version pinned there; null is the latest. */ version: string | null }
export type LibrarySkill = {
  id: string; name: string; /** `name` as words. */ title: string; description: string
  source: "custom" | "anthropic"; version: string; created_at: string; updated_at: string
  /** A skill the server ships. */
  default: boolean
  used_by: SkillUse[]
  /** Set for a read-only skill mirrored from a skills server. */
  server?: MirrorDoc | null
}
export type MirrorStatus = "current" | "pending" | "unavailable"
export type MirrorDoc = {
  server: string; uri: string; version_label: string | null; status: MirrorStatus
  pending_label: string | null; pending_changes: Record<"added" | "removed" | "changed", string[]> | null; last_checked_at: string | null
}
export type MirrorCheck = { result: "unchanged" | "updated" | "pending" | "unavailable" | "failed"; server: MirrorDoc | null }
/** A skill the node's server offers; `skill_id` once it is in the library, `taken` when another library skill holds its name. */
export type ServerSkillOption = { uri: string; name: string; description: string; skill_id: string | null; taken: boolean }
export type ServerSkillMenu = { label: string; skills: ServerSkillOption[] }
/** An agent's choice for one mirrored skill, and the newest version it can be pinned to. */
export type SkillFollow = { follow: boolean; status: MirrorStatus; version: string | null }
export type SkillDetail = LibrarySkill & { source_url: string | null }
export type SkillVersion = { version: string; created_at: string; author: string | null }
export type SyncedSkill = { name: string; action: "created" | "updated" | "skipped" | "failed"; detail: string | null }

export const ECOSYSTEMS = ["pip", "npm", "apt", "cargo", "gem", "go"] as const
export type Ecosystem = (typeof ECOSYSTEMS)[number]
export type EnvironmentDoc = {
  id: string; name: string; description: string; created_at: string
  /** The version loaded; a save sends it back and is refused when it moved on. */
  updated_at: string
  /** Read-only: a built-in agent runs in it. */
  managed: boolean
  cloud: boolean
  network: "limited" | "unrestricted"
  open_network: boolean
  allowed_hosts: string[]
  allow_package_managers: boolean
  allow_mcp_servers: boolean
  packages: Partial<Record<Ecosystem, string[]>>
  /** Channels and workspaces that pick it; absent when the server does not report them. */
  used_by?: { kind: "channel" | "workspace"; id: string; name: string; platform_kind?: string | null }[]
}
export type EnvironmentInput = {
  name: string; description: string; network: "limited" | "unrestricted"; allowed_hosts: string[]
  allow_package_managers: boolean; allow_mcp_servers: boolean; packages: Partial<Record<Ecosystem, string[]>>
}

/** What an agent remembers: whether it has shared memory, and (admins) a store per channel. */
export type MemoryPlace = { store: string; platform: string; name: string }
export type KnowledgeStores = { shared: boolean; channels: MemoryPlace[] }

/** An agent's own rule on one tool (`connection/tool`), for every channel; `revision` 0 is no rule yet. */
export type AgentToolRule = { tool: string; rule: "allow" | "ask" | "deny" | null; scope: "chat" | "chat_and_routines"; revision: number }
export type AgentToolRules = { active: boolean; rules: AgentToolRule[] }

/** The Google Workspace identity bound to an agent by its operator; shown, never changed here. */
export type GoogleBinding = { email: string; scopes: string[] }

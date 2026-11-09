// `/api/channels/*` shapes. USD amounts are numbers, except a budget's `limit_usd` ("10.00").
import type { ChannelKind } from "@/lib/format"
import type { EntityRef } from "@/lib/types"

/** The agents a response mentions, by the key it used; a key whose agent is gone has null id and name. */
export type AgentRefs = Record<string, EntityRef>

/** Where a cascaded value comes from; both null when nothing sets it. */
export type Sourced = { value: string | null; source: "channel" | "workspace" | "deployment" | null }

export type Readers = "any" | "inside" | "own"
export type Writers = "any" | "own" | "none"
export type BudgetWindow = "monthly" | "total" | "fixed"
/** `locked`: only its own agents read it (confidential, admins only). `shared`: shared with another organisation. */
export type ChannelTag = "kept_inside" | "locked" | "shared"

export type ChannelBudget = {
  limit_usd: string
  window: BudgetWindow
  starts_at: string | null
  ends_at: string | null
  spent_usd: number
  remaining_usd: number
  percent_used: number
  active: boolean
  exceeded: boolean
}

/** An ancestor in a channel's `path`. */
export type PathNode = { id: string; name: string | null; kind: ChannelKind }

/** One channel; the web chat row has `id` "web", `platform` "web" and `name` "Web chat". */
export type Channel = {
  id: string
  platform: string
  name: string | null
  kind: ChannelKind
  /** Only "active" rows are listed; gone and direct ones are kept out. */
  status: "active" | "gone" | "direct"
  parent: { id: string; name: string | null } | null
  /** Ancestors, root first: category and channel, or team. */
  path: PathNode[]
  last_seen_at: string | null
  agent: Sourced
  environment: Sourced
  readers: Readers
  writers: Writers
  kept_agents: string[]
  tags: ChannelTag[]
  /** Budget and spend are null for a member. */
  budget: ChannelBudget | null
  spend: { month_usd: number; last_30_days_usd: number } | null
  last_active_at: string | null
}

/** GET /api/channels: admins see every channel; members only those their chat platform shows them. */
export type ChannelList = {
  platform: string; default_agent: Sourced; default_environment: Sourced; channels: Channel[]; agents: AgentRefs
}

export type Choice<T extends string> = { value: T; label: string }

/** GET /api/channels/:id */
export type ChannelDetail = Channel & {
  readers_label: string
  writers_label: string
  readers_options: Choice<Readers>[]
  writers_options: Choice<Writers>[]
  can_manage: boolean
  budget_editable: boolean
  agents: AgentRefs
}

/** Every write answers with the channel as it is now; `notes` are facts to show once. A value read is stale: 409. */
export type ChannelChangeResult = { channel: ChannelDetail; notes: string[] }

/** PUT /:id/default-agent. `expected` is `agent.value` when `agent.source` is "channel", else null. */
export type DefaultAgentChange = { agent: string | null; expected: string | null }
/** POST /:id/kept-agents. `expected` is `kept_agents` as read. */
export type KeptAgentChange = { action: "add" | "remove"; agent: string; expected: string[] }
/**
 * PUT /:id/access. `copy_agent` copies the default agent so it can be kept here.
 * Moving readers off "own" while agents are kept here needs one of `release_agents` (drop their rule)
 * or `keep_rules` (keep it); neither, or both, is a 422.
 */
export type AccessChange = {
  readers?: Readers
  writers?: Writers
  expected: { readers: Readers; writers: Writers }
  copy_agent?: boolean
  copy_from?: string | null
  release_agents?: boolean
  keep_rules?: boolean
}
/** PUT /:id/budget. `expected` is the budget as read (limit, window, dates), or null. */
export type BudgetChange = {
  action: "set" | "raise" | "clear"
  limit_usd?: string
  window?: BudgetWindow
  starts_at?: string | null
  ends_at?: string | null
  amount_usd?: string
  expected: Pick<ChannelBudget, "limit_usd" | "window" | "starts_at" | "ends_at"> | null
}

/** GET /:id/history (admins): entries newest first, and the agents their values name. `tool_rule` values are `{agent, tool, rule}`. */
export type ChannelHistory = { entries: ChannelHistoryEntry[]; agents: AgentRefs }
export type ChannelHistoryEntry = {
  at: string
  setting: ChannelSetting
  before: unknown
  after: unknown
  actor: { id: string; name: string | null } | null
  surface: "web" | "chat" | "panel" | "cli" | null
}

export type ChannelSetting =
  | "default_agent" | "kept_agents" | "access" | "budget" | "participation" | "progress_plans" | "environment"
  | "skills" | "provider_pin" | "channel_copy" | "thread_participation" | "thread_progress_plans" | "thread_rule"
  | "thread_handoff" | "tool_rule" | "approvals" | "thread_approvals" | "cost_footer"

export type SwitchMode = "on" | "off" | "disabled"
/** Following threads or progress plans. `own` is this tier's own mode (null: inherits); `value` and `source` are what applies. */
export type ChannelSwitch = {
  available: boolean
  own: SwitchMode | null
  value: SwitchMode
  source: "deployment" | "workspace" | "channel" | "thread"
}
export type ApprovalMode = "on" | "off"
/**
 * Whether third-party writes wait for an Approve card here. `own` is this tier's own value (null: inherits);
 * `value` and `source` are what applies. `active` is false until the operator enables approval overrides,
 * so a saved value is ignored. `editable` needs a linked chat account; the server checks it is a chat admin.
 */
export type ChannelApprovals = {
  available: boolean
  own: ApprovalMode | null
  value: ApprovalMode
  source: "default" | "server" | "channel" | "thread"
  active: boolean
  editable: boolean
}
export type CostFooter = { shown: boolean; editable: boolean }
export type ThreadOverrideKind = "participation" | "progress_plans" | "thread_rule" | "handoff"
export type ToolRule = { agent: string; tool: string; tool_label: string; server_name: string; rule: "allow" | "ask" | "deny" | null; scope: "chat" | "chat_and_routines"; revision: number }
/** `key` is a day (YYYY-MM-DD), an agent id or a person id; null when unknown. */
export type SpendAmount = { key: string | null; name: string | null; usd: number }

/** GET /:id/settings (members and admins). */
export type ChannelSettings = {
  participation: ChannelSwitch
  progress_plans: ChannelSwitch
  approvals: ChannelApprovals
  skills: { skill_id: string; name: string }[]
  /** `editable` is false while tool safety is off, or for members. */
  tool_rules: { editable: boolean; rules: ToolRule[] }
  provider_pin: { backend: string; model: string | null; model_label: string | null; local: number } | null
  channel_copy: { name: string; agent_id: string } | null
  /** Threads with their own setting; `value` is the mode, "readers/writers", or the agent handed to. */
  threads: { thread_id: string; name: string | null; kind: ThreadOverrideKind; value: string }[]
  link: string | null
  /** Teams private and shared channels only. */
  teams_subscription: { status: "active" | "expired" | "missing"; expires_at: string | null } | null
  routines: { id: string; title: string; agent: string; cron: string; enabled: boolean; relation: "posts_here" | "made_here" | "both" }[]
  spend: { days: number; by_day: SpendAmount[]; by_agent: SpendAmount[]; by_person: SpendAmount[] } | null
  agents: AgentRefs
}

/** PUT /:id/environment, /api/channel-defaults/environment. A 428 asks to resend with `confirm_open_network`. */
export type EnvironmentChange = { environment: string | null; expected: string | null; confirm_open_network?: boolean }
/** POST /:id/skills. `skill` is a name or id to add, the skill id to remove; `expected` the skill ids as read. */
export type SkillChange = { action: "add" | "remove"; skill: string; expected: string[] }
/** PUT /:id/tool-rules. null rule clears; `expected_revision` is the rule's revision as read (0 when none). */
export type ToolRuleChange = { agent: string; tool: string; rule: ToolRule["rule"]; scope?: ToolRule["scope"]; expected_revision: number }
/** POST /:id/provider-pin/clear. */
export type PinClear = { expected_local: number }
/** POST /:id/channel-copy/retire. */
export type CopyRetire = { name: string; agent_id: string }
/** POST /:id/threads/clear. */
export type ThreadClear = { thread_id: string; kind: ThreadOverrideKind }

/** GET /api/channel-defaults; every PUT under it answers with this too. */
export type ChannelDefaults = {
  platform: string
  agent: Sourced
  environment: Sourced
  participation: ChannelSwitch
  progress_plans: ChannelSwitch
  approvals: ChannelApprovals
  cost_footer: CostFooter
}
/** PUT /api/channel-defaults/agent. */
export type WorkspaceAgentChange = { agent: string | null; expected: string | null }

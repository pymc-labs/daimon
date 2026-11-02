// `/api/channels/*` shapes. USD amounts are numbers, except a budget's `limit_usd` ("10.00").

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

/** One channel; the web chat row has `id` "web", `platform` "web" and `name` "Web chat". */
export type Channel = {
  id: string
  platform: string
  name: string | null
  kind: string | null
  parent: { id: string; name: string | null } | null
  last_seen_at: string | null
  agent: Sourced
  environment: Sourced
  readers: Readers
  writers: Writers
  kept_agents: string[]
  tags: ChannelTag[]
  budget: ChannelBudget | null
  spend: { month_usd: number; last_30_days_usd: number }
  last_active_at: string | null
}

/** GET /api/channels: admins see every channel; members only those their chat platform shows them. */
export type ChannelList = { platform: string; default_agent: Sourced; default_environment: Sourced; channels: Channel[] }

export type Choice<T extends string> = { value: T; label: string }

/** GET /api/channels/:id */
export type ChannelDetail = Channel & {
  readers_label: string
  writers_label: string
  readers_options: Choice<Readers>[]
  writers_options: Choice<Writers>[]
  can_manage: boolean
  budget_editable: boolean
}

/** Every write answers with the channel as it is now; `notes` are facts to show once. A value read is stale: 409. */
export type ChannelChangeResult = { channel: ChannelDetail; notes: string[] }

/** PUT /:id/default-agent. `expected` is `agent.value` when `agent.source` is "channel", else null. */
export type DefaultAgentChange = { agent: string | null; expected: string | null }
/** POST /:id/kept-agents. `expected` is `kept_agents` as read. */
export type KeptAgentChange = { action: "add" | "remove"; agent: string; expected: string[] }
/** PUT /:id/access. `copy_agent` copies the default agent so it can be kept here; `release_agents` lets kept agents go. */
export type AccessChange = {
  readers?: Readers
  writers?: Writers
  expected: { readers: Readers; writers: Writers }
  copy_agent?: boolean
  copy_from?: string | null
  release_agents?: boolean
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

/** GET /:id/history (admins), newest first. `tool_rule` rows have `{agent, tool, rule}` values and no surface. */
export type ChannelHistoryEntry = {
  at: string
  setting: "default_agent" | "kept_agents" | "access" | "budget" | "tool_rule"
  before: unknown
  after: unknown
  actor: { id: string; name: string | null } | null
  surface: "web" | "chat" | "panel" | "cli" | null
}

// Pure helpers for the channels module: grouping, filters, confirm triggers and history text.
import { type ChannelKind, channelLabel, ENTITY_FALLBACK, modelLabel, toolLabel } from "@/lib/format"
import type { EntityRef } from "@/lib/types"
import { fromCron, WEEKDAYS } from "@/modules/routines/cron"
import type { TreeRow } from "@/components/tree-state"
import type {
  AgentRefs, Channel, ChannelBudget, ChannelDetail, ChannelHistoryEntry, ChannelSwitch, ChannelTag, Choice, PathNode, Readers, SpendAmount, Sourced,
  SwitchChange, SwitchMode, ThreadOverrideKind, ToolRule, ToolRuleChange, Writers,
} from "./types"

export const PLATFORMS: Record<string, string> = { web: "Web chat", discord: "Discord", slack: "Slack", teams: "Teams" }
export const platformLabel = (p: string) => PLATFORMS[p] ?? p

export const TAGS: Record<ChannelTag, string> = { kept_inside: "Kept inside", locked: "Locked", shared: "Shared" }
export const ACCESS_FILTERS: { value: string; label: string }[] = [
  { value: "open", label: "Open" }, ...Object.entries(TAGS).map(([value, label]) => ({ value, label })),
]
export const WINDOWS = { monthly: "Monthly", total: "Total", fixed: "Fixed dates" } as const


/** A tree row: a real channel, thread or post, or a group that only folds (a category, a team, or a channel the list does not hold). */
export type Node =
  | { type: "channel"; channel: Channel }
  | { type: "group"; id: string; name: string | null; kind: ChannelKind; platform: string }
export type ChannelNode = TreeRow<Node>

/**
 * The directory as a tree from each row's `path`: category or team, then channel, then thread.
 * A thread whose channel is not a row sits under a group for that channel. Web chat comes first;
 * with several platforms roots sort by platform, then channels before groups, then name.
 */
export function channelTree(rows: Channel[]): ChannelNode[] {
  const listed = new Map(rows.map((c) => [`${c.platform}:${c.id}`, c]))
  const groups = new Map<string, ChannelNode>()
  const refer = (platform: string, path: PathNode[]): string | null => {
    const node = path.at(-1)
    if (!node) return null
    const row = listed.get(`${platform}:${node.id}`)
    if (row && node.kind !== "category" && node.kind !== "team") return row.id
    const id = `g:${platform}:${node.id}`
    if (!groups.has(id)) groups.set(id, { id, parentId: refer(platform, path.slice(0, -1)), kind: "group", row: { type: "group", id, name: node.name, kind: node.kind, platform } })
    return id
  }
  const items: ChannelNode[] = rows.map((c) => ({ id: c.id, parentId: refer(c.platform, c.path), kind: "item", row: { type: "channel", channel: c } }))
  const multi = new Set(rows.map((c) => c.platform)).size > 1
  const platformOf = (n: ChannelNode) => (n.row.type === "channel" ? n.row.channel.platform : n.row.platform)
  const labelOf = (n: ChannelNode) => (n.row.type === "channel" ? (n.row.channel.name ?? "") : (n.row.name ?? ""))
  const rank = (n: ChannelNode) => (platformOf(n) === "web" ? 0 : n.kind === "item" ? 1 : 2)
  const order = (a: ChannelNode, b: ChannelNode) =>
    rank(a) - rank(b) || (multi ? platformOf(a).localeCompare(platformOf(b)) : 0) || labelOf(a).localeCompare(labelOf(b))
  return [...items, ...groups.values()].sort(order)
}

export type Filters = { q: string; platforms: string[]; access: string[]; budget: string[]; agents: string[]; override?: string | null }

export const OVERRIDES: Record<string, string> = { agent: "Default agent", sandbox: "Sandbox" }

/** Whether a channel sets `what` itself instead of taking the workspace's value. */
export const overrides = (c: Channel, what: string) =>
  what === "agent" ? c.agent.source === "channel" : what === "sandbox" ? c.environment.source === "channel" : true

/** How many channels set `what` themselves. */
export const overrideCount = (rows: Channel[], what: string) => rows.filter((c) => overrides(c, what)).length

const active = (f: Filters) => !!(f.q.trim() || f.platforms.length || f.access.length || f.budget.length || f.agents.length || f.override)

/** Within a facet any pick matches; across facets all must. */
export function matchChannel(c: Channel, f: Filters): boolean {
  const q = f.q.trim().toLowerCase()
  return (!q || (c.name ?? nameOf(c)).toLowerCase().includes(q))
    && (!f.platforms.length || f.platforms.includes(c.platform))
    && (!f.access.length || f.access.some((a) => (a === "open" ? !c.tags.length : c.tags.includes(a as ChannelTag))))
    && (!f.budget.length || f.budget.some((b) => (b === "yes" ? !!c.budget : !c.budget)))
    && (!f.agents.length || (c.agent.value !== null && f.agents.includes(c.agent.value)))
    && (!f.override || overrides(c, f.override))
}

export const filterChannels = (rows: Channel[], f: Filters): Channel[] => rows.filter((c) => matchChannel(c, f))

/** The tree filter, or null with nothing to filter by. A group matches on its name alone, and only for a plain search. */
export function treeFilter(f: Filters): ((n: Node) => boolean) | null {
  if (!active(f)) return null
  const q = f.q.trim().toLowerCase()
  const plain = !!q && !f.platforms.length && !f.access.length && !f.budget.length && !f.agents.length && !f.override
  return (n) => (n.type === "channel" ? matchChannel(n.channel, f) : plain && (n.name ?? "").toLowerCase().includes(q))
}

/** The agent behind a key: null for no key, an empty ref for one whose agent is gone. */
export const agentOf = (refs: AgentRefs | undefined, key: string | null): EntityRef | null =>
  key === null ? null : (refs?.[key] ?? { id: null, name: null })

/** An agent key as its name; "Deleted agent" when it has none. */
export const agentName = (refs: AgentRefs | undefined, key: string) => agentOf(refs, key)?.name ?? ENTITY_FALLBACK.agent

/** Agent keys in use, with the name each reads as. */
export const agentsOf = (rows: Channel[], refs?: AgentRefs) =>
  [...new Set(rows.flatMap((c) => (c.agent.value ? [c.agent.value] : [])))]
    .map((key) => ({ key, name: agentName(refs, key) }))
    .sort((a, b) => a.name.localeCompare(b.name))

export type AgentChoice = { key: string; name: string; avatar: string | null }

/**
 * Agents to pick from. The roster is keyed by name; a key already in use that names the same agent keeps its own key,
 * and a key whose agent is gone is listed as "Deleted agent" so it stays visible.
 */
export function agentChoices(roster: { name: string; avatar: string | null }[], current: string[], refs: AgentRefs | undefined): AgentChoice[] {
  const used = new Map<string, string>()
  const out: AgentChoice[] = []
  for (const key of current) {
    const ref = agentOf(refs, key)
    if (ref?.name) used.set(ref.name, key)
    else out.push({ key, name: ENTITY_FALLBACK.agent, avatar: null })
  }
  for (const a of roster) out.push({ key: used.get(a.name) ?? a.name, name: a.name, avatar: a.avatar })
  return out
}

export type Ask = { title: string; body?: string; action: string; destructive?: boolean; alternate?: string }

/** The confirm a rule change needs, or null when it applies at once. */
export function accessAsk(
  c: Pick<ChannelDetail, "readers" | "writers" | "agent" | "kept_agents">,
  next: { readers?: Readers; writers?: Writers },
): Ask | null {
  const own = next.readers === "own" ? next.readers !== c.readers : next.writers === "own" && next.writers !== c.writers
  if (own) {
    const name = c.agent.value
    const mine = c.agent.source === "channel" && !!name && c.kept_agents.includes(name)
    return {
      title: "Only its own agents?",
      body: name ? (mine ? `${name} stays here.` : `A copy of ${name} is made for this channel.`) : "This channel needs an agent of its own.",
      action: "Continue",
    }
  }
  if (c.readers === "own" && next.readers && next.readers !== "own") {
    return {
      title: "Leave its own agents?",
      body: c.kept_agents.length ? `${c.kept_agents.join(", ")} may run anywhere again.` : undefined,
      action: "Release agents",
      ...(c.kept_agents.length > 0 && { alternate: "Keep their rules" }),
    }
  }
  if (next.writers === "none" && c.writers !== "none") {
    return { title: "Nobody can post?", body: "Agents stop replying here, admins included.", action: "Lock", destructive: true }
  }
  return null
}

/** Spend a new limit is compared with: the budget's own while its window holds, else this month's. */
export function spentFor(c: Pick<Channel, "budget" | "spend">, window: ChannelBudget["window"]) {
  const own = c.budget && c.budget.window === window ? c.budget.spent_usd : 0
  return Math.max(own, window === "monthly" ? (c.spend?.month_usd ?? 0) : 0)
}

/** A limit at or below spend stops new turns at once. */
export const budgetStops = (limit: number, spent: number) => limit > 0 && limit <= spent

/** A piece of a history line: text, or an agent key to show as a link. */
export type Part = string | { agent: string }
export type Change = { what: string; agent?: string; from: Part[]; to: Part[] }

const none: Part[] = ["None"]
const text = (v: unknown): Part[] => (v == null || v === "" ? none : [String(v)])
const agents = (keys: string[]): Part[] => keys.flatMap((k, i): Part[] => (i ? [", ", { agent: k }] : [{ agent: k }]))

/** One history row as what changed, from and to; agent keys stay keys so the page can link them. */
export function describeChange(e: ChannelHistoryEntry, labels: { readers: Choice<Readers>[]; writers: Choice<Writers>[] }): Change {
  const pick = <T extends string>(opts: Choice<T>[], v: unknown) => opts.find((o) => o.value === v)?.label ?? String(v ?? "None")
  const obj = (v: unknown) => (v && typeof v === "object" ? (v as Record<string, unknown>) : null)
  const budget = (v: unknown): Part[] => {
    const b = obj(v)
    return b ? [`$${b.limit_usd} ${String(WINDOWS[b.window as keyof typeof WINDOWS] ?? b.window).toLowerCase()}`] : none
  }
  const keys = (v: unknown) => (Array.isArray(v) ? (v as string[]) : [])
  const key = (v: unknown): Part[] => (typeof v === "string" && v ? agents([v]) : none)
  switch (e.setting) {
    case "default_agent": return { what: "Default agent", from: key(e.before), to: key(e.after) }
    case "kept_agents": {
      const [b, a] = [keys(e.before), keys(e.after)]
      const added = a.filter((x) => !b.includes(x)), removed = b.filter((x) => !a.includes(x))
      return { what: "Kept here", from: removed.length ? ["removed ", ...agents(removed)] : [], to: added.length ? ["added ", ...agents(added)] : [] }
    }
    case "access": {
      const [b, a] = [obj(e.before), obj(e.after)]
      if (b?.readers !== a?.readers) return { what: "Who can read", from: [pick(labels.readers, b?.readers)], to: [pick(labels.readers, a?.readers)] }
      return { what: "Who can post", from: [pick(labels.writers, b?.writers)], to: [pick(labels.writers, a?.writers)] }
    }
    case "budget": return { what: "Budget", from: budget(e.before), to: budget(e.after) }
    case "tool_rule": {
      const [b, a] = [obj(e.before), obj(e.after)]
      const t = a ?? b
      const [server, tool] = String(t?.tool ?? "").split("/")
      return { what: tool ? toolLabel(server!, tool) : toolLabel(server ?? "", "").replace(/^: |: $/g, ""), ...(typeof t?.agent === "string" && { agent: t.agent }), from: text(b?.rule), to: text(a?.rule) }
    }
    case "skills": return { what: "Skills", from: list(e.before), to: list(e.after) }
    case "provider_pin": {
      const model = (v: unknown): Part[] => { const o = obj(v); return o ? [o.model ? modelLabel(String(o.model)) : String(o.backend ?? "None")] : none }
      return { what: "Model", from: model(e.before), to: model(e.after) }
    }
    case "thread_handoff": {
      const v = (x: unknown): Part[] => { const o = obj(x); const k = o?.value ?? o?.agent ?? x; return typeof k === "string" ? agents([k]) : none }
      return { what: OTHER.thread_handoff!, from: v(e.before), to: v(e.after) }
    }
    default: {
      const thread = (v: unknown): Part[] => (obj(v) ? text(obj(v)!.mode ?? obj(v)!.value) : text(v))
      return { what: OTHER[e.setting] ?? e.setting.replaceAll("_", " "), from: thread(e.before), to: thread(e.after) }
    }
  }
}

const list = (v: unknown): Part[] => (Array.isArray(v) && v.length ? [v.join(", ")] : none)
const OTHER: Record<string, string> = {
  participation: "Joins threads", progress_plans: "Progress plans", environment: "Sandbox", channel_copy: "Channel copy",
  thread_participation: "Joins in a thread", thread_progress_plans: "Progress plans in a thread",
  thread_rule: "Thread access", thread_handoff: "Thread handed to",
}

/** A channel as people write it; web chat keeps its own name. */
export const nameOf = (c: { platform: string; name: string | null; kind?: string | null }) =>
  c.platform === "web" ? (c.name ?? "Web chat") : channelLabel(c.name, c.kind)

/** What a rule change sends besides the new rule: copy the agent for "own", and say what happens to agents when leaving it. */
export function accessFlags(
  c: Pick<ChannelDetail, "readers" | "kept_agents">,
  next: { readers?: Readers; writers?: Writers },
  pick: "action" | "alternate" | null = "action",
) {
  const toOwn = next.readers === "own" || next.writers === "own"
  const leaves = c.readers === "own" && !!next.readers && next.readers !== "own"
  return {
    ...(toOwn && { copy_agent: true }),
    ...(leaves && (pick === "alternate" ? { keep_rules: true } : { release_agents: true })),
  }
}

export const SWITCH_VALUE: Record<SwitchMode, string> = { on: "On", off: "Off", disabled: "Disabled" }
export const SWITCH_SOURCE: Record<ChannelSwitch["source"], string> = {
  deployment: "Built-in default", workspace: "Workspace default", channel: "Set here", thread: "Set in a thread",
}
export type SwitchChoice = "inherit" | SwitchMode

export const choiceOf = (s: Pick<ChannelSwitch, "own">): SwitchChoice => s.own ?? "inherit"

/** The write for a pick; `inherit` clears this tier's own value. `expected` is the own value as read. */
export const switchChange = (choice: SwitchChoice, s: Pick<ChannelSwitch, "own">): SwitchChange =>
  ({ mode: choice === "inherit" ? null : choice, expected: s.own })

/** What "not available" reads as for a switch: participation is on Discord and Teams only. */
export const unavailableText = (what: "participation" | "progress_plans", platform: string) =>
  what === "participation" ? `Not on ${platformLabel(platform)}` : "Not available"

const DAY_MS = 86_400_000
const dayKey = (t: number) => new Date(t).toISOString().slice(0, 10)

/** One bar per day for the last `days` days ending today (UTC), days with no spend as zero. */
export function fillDays(rows: SpendAmount[], days: number, now: number): { day: string; usd: number }[] {
  const byDay = new Map(rows.map((r) => [r.key, r.usd]))
  const today = Math.floor(now / DAY_MS) * DAY_MS
  return Array.from({ length: days }, (_, i) => {
    const day = dayKey(today - (days - 1 - i) * DAY_MS)
    return { day, usd: byDay.get(day) ?? 0 }
  })
}

export type SpendLine = { key: string; label: string; usd: number; share: number }

/** Biggest first with each line's share of the total; spend with no id reads as `none`. */
export function spendLines(rows: SpendAmount[], none: string): SpendLine[] {
  const total = rows.reduce((t, r) => t + r.usd, 0)
  return rows
    .map((r) => ({ key: r.key ?? "none", label: r.name ?? (r.key ? "Unknown" : none), usd: r.usd, share: total > 0 ? r.usd / total : 0 }))
    .sort((a, b) => b.usd - a.usd || a.label.localeCompare(b.label))
}

/** A routine's schedule in words; shapes the picker cannot draw show the cron expression. */
export function cronWords(cron: string): string {
  const s = fromCron(cron)
  switch (s.kind) {
    case "hourly": return `Every hour at :${s.time.slice(3)}`
    case "daily": return `Every day at ${s.time}`
    case "weekdays": return `Weekdays at ${s.time}`
    case "weekly": return `${WEEKDAYS[s.weekday]}s at ${s.time}`
    case "monthly": return `Day ${s.day} at ${s.time}`
    case "custom": return cron
  }
}

export const RELATIONS = { posts_here: "Posts here", made_here: "Made here", both: "Made and posts here" } as const

export const THREAD_KINDS: Record<ThreadOverrideKind, string> = {
  participation: "Following threads", progress_plans: "Progress plans", thread_rule: "Thread rule", handoff: "Handed to",
}

/** A thread override's value as a label: a switch mode, "readers/writers", or the agent handed to. */
export function threadValue(kind: ThreadOverrideKind, value: string, labels: { readers: Choice<Readers>[]; writers: Choice<Writers>[] }) {
  if (kind === "participation" || kind === "progress_plans") return SWITCH_VALUE[value as SwitchMode] ?? value
  if (kind === "thread_rule") {
    const [r, w] = value.split("/")
    return `${labels.readers.find((o) => o.value === r)?.label ?? r}, ${labels.writers.find((o) => o.value === w)?.label ?? w}`
  }
  return value
}

export const SUBSCRIPTION = { active: "Active", expired: "Expired", missing: "Missing" } as const

/** A rule's tool as people read it: "Search: Web search", or just the tool when it has no server. */
export const toolName = (r: Pick<ToolRule, "server_name" | "tool_label">) => (r.server_name ? `${r.server_name}: ${r.tool_label}` : r.tool_label)

export const RULES = { allow: "Allow", ask: "Ask", deny: "Deny" } as const
export const SCOPES = { chat: "Chat", chat_and_routines: "Chat and routines" } as const

/** The write for a rule: `expected_revision` is the revision of the same agent and tool as read, 0 when none. */
export function toolRuleChange(
  next: Pick<ToolRule, "agent" | "tool" | "rule"> & { scope?: ToolRule["scope"] },
  rules: ToolRule[],
): ToolRuleChange {
  const prior = rules.find((r) => r.agent === next.agent && r.tool === next.tool)
  return { agent: next.agent, tool: next.tool, rule: next.rule, scope: next.scope ?? prior?.scope, expected_revision: prior?.revision ?? 0 }
}

const DEFAULT_SOURCE = { workspace: "Workspace", deployment: "Built-in default" } as const

/** Where a workspace default comes from, shown apart from its value. */
export const defaultSource = (s: { source: Sourced["source"] | ChannelSwitch["source"] }) =>
  s.source === "workspace" || s.source === "deployment" ? DEFAULT_SOURCE[s.source] : null

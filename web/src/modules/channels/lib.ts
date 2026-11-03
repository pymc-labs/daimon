// Pure helpers for the channels module: grouping, filters, confirm triggers and history text.
import { channelLabel } from "@/lib/format"
import { fromCron, WEEKDAYS } from "@/modules/routines/cron"
import type {
  Channel, ChannelBudget, ChannelDetail, ChannelHistoryEntry, ChannelSwitch, ChannelTag, Choice, Readers, SpendAmount, Sourced,
  SwitchChange, SwitchMode, ThreadOverrideKind, ToolRule, ToolRuleChange, Writers,
} from "./types"

export const PLATFORMS: Record<string, string> = { web: "Web chat", discord: "Discord", slack: "Slack", teams: "Teams" }
export const platformLabel = (p: string) => PLATFORMS[p] ?? p

export const TAGS: Record<ChannelTag, string> = { kept_inside: "Kept inside", locked: "Locked", shared: "Shared" }
export const ACCESS_FILTERS: { value: string; label: string }[] = [
  { value: "open", label: "Open" }, ...Object.entries(TAGS).map(([value, label]) => ({ value, label })),
]
export const WINDOWS = { monthly: "Monthly", total: "Total", fixed: "Fixed dates" } as const

export const SOURCE_LABELS: Record<NonNullable<Sourced["source"]>, string> = { channel: "Set here", workspace: "Workspace default", deployment: "Built-in default" }

/** An agent or sandbox value with where it comes from: "Workspace default: Daimon". */
export function sourcedLabel(s: Sourced) {
  if (!s.source) return s.value ?? "None"
  return s.source === "channel" ? (s.value ?? "None") : `${SOURCE_LABELS[s.source]}: ${s.value ?? "None"}`
}

export type Group = { key: string; name: string | null; rows: Channel[] }

const byName = (a: Channel, b: Channel) => (a.name ?? "").localeCompare(b.name ?? "")

/** Rows under their category or team; web chat and channels with no parent come first, without a header. */
export function groupChannels(rows: Channel[]): Group[] {
  const loose: Channel[] = []
  const groups = new Map<string, Group>()
  for (const c of rows) {
    if (!c.parent) { loose.push(c); continue }
    const key = `${c.platform}:${c.parent.id}`
    const g = groups.get(key) ?? { key, name: c.parent.name ?? "Other", rows: [] }
    g.rows.push(c)
    groups.set(key, g)
  }
  loose.sort((a, b) => (a.platform === "web" ? -1 : b.platform === "web" ? 1 : byName(a, b)))
  const named = [...groups.values()].sort((a, b) => (a.name ?? "").localeCompare(b.name ?? ""))
  for (const g of named) g.rows.sort(byName)
  return [...(loose.length ? [{ key: "loose", name: null, rows: loose }] : []), ...named]
}

export type Filters = { q: string; platforms: string[]; access: string[]; budget: string[]; agents: string[] }

/** Within a facet any pick matches; across facets all must. */
export function filterChannels(rows: Channel[], f: Filters): Channel[] {
  const q = f.q.trim().toLowerCase()
  return rows.filter((c) =>
    (!q || `${c.name ?? ""} ${c.parent?.name ?? ""}`.toLowerCase().includes(q))
    && (!f.platforms.length || f.platforms.includes(c.platform))
    && (!f.access.length || f.access.some((a) => (a === "open" ? !c.tags.length : c.tags.includes(a as ChannelTag))))
    && (!f.budget.length || f.budget.some((b) => (b === "yes" ? !!c.budget : !c.budget)))
    && (!f.agents.length || (c.agent.value !== null && f.agents.includes(c.agent.value))))
}

export const agentsOf = (rows: Channel[]) => [...new Set(rows.flatMap((c) => (c.agent.value ? [c.agent.value] : [])))].sort()

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
  return Math.max(own, window === "monthly" ? c.spend.month_usd : 0)
}

/** A limit at or below spend stops new turns at once. */
export const budgetStops = (limit: number, spent: number) => limit > 0 && limit <= spent

const word = (s: string) => s.replaceAll("_", " ")
const quote = (v: unknown) => (v == null || v === "" ? "none" : String(v))

/** One history row as a plain sentence part: what changed, from and to. */
export function describeChange(e: ChannelHistoryEntry, labels: { readers: Choice<Readers>[]; writers: Choice<Writers>[] }) {
  const pick = <T extends string>(opts: Choice<T>[], v: unknown) => opts.find((o) => o.value === v)?.label ?? quote(v)
  const obj = (v: unknown) => (v && typeof v === "object" ? (v as Record<string, unknown>) : null)
  const budget = (v: unknown) => {
    const b = obj(v)
    return b ? `$${b.limit_usd} ${String(WINDOWS[b.window as keyof typeof WINDOWS] ?? b.window).toLowerCase()}` : "none"
  }
  switch (e.setting) {
    case "default_agent": return { what: "Default agent", from: quote(e.before), to: quote(e.after) }
    case "kept_agents": {
      const [b, a] = [e.before, e.after].map((v) => (Array.isArray(v) ? (v as string[]) : []))
      const added = a!.filter((x) => !b!.includes(x)), removed = b!.filter((x) => !a!.includes(x))
      return { what: "Kept agents", from: removed.length ? `removed ${removed.join(", ")}` : "", to: added.length ? `added ${added.join(", ")}` : "" }
    }
    case "access": {
      const [b, a] = [obj(e.before), obj(e.after)]
      if (b?.readers !== a?.readers) return { what: "Who can read", from: pick(labels.readers, b?.readers), to: pick(labels.readers, a?.readers) }
      return { what: "Who can post", from: pick(labels.writers, b?.writers), to: pick(labels.writers, a?.writers) }
    }
    case "budget": return { what: "Budget", from: budget(e.before), to: budget(e.after) }
    case "tool_rule": {
      const [b, a] = [obj(e.before), obj(e.after)]
      const t = a ?? b
      return { what: `Tool rule ${word(quote(t?.tool))}`, from: quote(b?.rule), to: quote(a?.rule) }
    }
    case "skills": return { what: "Skills", from: list(e.before), to: list(e.after) }
    case "provider_pin": return { what: "Provider", from: quote(obj(e.before)?.backend), to: quote(obj(e.after)?.backend) }
    default: {
      const thread = (v: unknown) => (obj(v) ? quote(obj(v)!.mode ?? obj(v)!.value) : quote(v))
      return { what: OTHER[e.setting], from: thread(e.before), to: thread(e.after) }
    }
  }
}

const list = (v: unknown) => (Array.isArray(v) && v.length ? v.join(", ") : "none")
const OTHER: Record<string, string> = {
  participation: "Following threads", progress_plans: "Progress plans", environment: "Sandbox", channel_copy: "Channel copy",
  thread_participation: "Following in a thread", thread_progress_plans: "Progress plans in a thread",
  thread_rule: "Thread rule", thread_handoff: "Thread hand-over",
}

/** A channel as people write it; web chat keeps its own name. */
export const nameOf = (c: { platform: string; name: string | null }) => (c.platform === "web" ? (c.name ?? "Web chat") : channelLabel(c.name))

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

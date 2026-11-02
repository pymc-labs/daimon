// Pure helpers for the channels module: grouping, filters, confirm triggers and history text.
import { channelLabel } from "@/lib/format"
import type {
  Channel, ChannelBudget, ChannelDetail, ChannelHistoryEntry, ChannelTag, Choice, Readers, Sourced, Writers,
} from "./types"

export const PLATFORMS: Record<string, string> = { web: "Web chat", discord: "Discord", slack: "Slack", teams: "Teams" }
export const platformLabel = (p: string) => PLATFORMS[p] ?? p

export const TAGS: Record<ChannelTag, string> = { kept_inside: "Kept inside", locked: "Locked", shared: "Shared" }
export const ACCESS_FILTERS: { value: string; label: string }[] = [
  { value: "open", label: "Open" }, ...Object.entries(TAGS).map(([value, label]) => ({ value, label })),
]
export const WINDOWS = { monthly: "Monthly", total: "Total", fixed: "Fixed dates" } as const

const SOURCE: Record<NonNullable<Sourced["source"]>, string> = { channel: "Set here", workspace: "Workspace default", deployment: "Built-in default" }

/** An agent or sandbox value with where it comes from: "Workspace default: Daimon". */
export function sourcedLabel(s: Sourced) {
  if (!s.source) return s.value ?? "None"
  return s.source === "channel" ? (s.value ?? "None") : `${SOURCE[s.source]}: ${s.value ?? "None"}`
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

export type Ask = { title: string; body?: string; action: string; destructive?: boolean }

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
  }
}

/** A channel as people write it; web chat keeps its own name. */
export const nameOf = (c: { platform: string; name: string | null }) => (c.platform === "web" ? (c.name ?? "Web chat") : channelLabel(c.name))

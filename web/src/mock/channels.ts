// Channels: the workspace's channels, one channel, each section's change and its history.
import { http, HttpResponse, type HttpHandler } from "msw"

import type {
  AccessChange, BudgetChange, Channel, ChannelDetail, ChannelHistoryEntry, ChannelList, DefaultAgentChange, KeptAgentChange, Readers, Writers,
} from "@/modules/channels/types"
import { forbidden, notFound, wait } from "./util"
import { me } from "./world"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const READERS: Record<Readers, string> = { any: "Anyone", inside: "Only this channel", own: "Only its own agents" }
const WRITERS: Record<Writers, string> = { any: "Anyone", own: "Only its own agents", none: "Nobody" }
const WORKSPACE = { value: "daimon", source: "workspace" } as const

const C = (id: string, name: string | null, parent: string | null, more: Partial<Channel> = {}): Channel => ({
  id, platform: "discord", name, kind: "text", parent: parent ? { id: `k_${parent}`, name: parent } : null, last_seen_at: ago(2),
  agent: WORKSPACE, environment: { value: "default", source: "deployment" }, readers: "any", writers: "any", kept_agents: [], tags: [],
  budget: null, spend: { month_usd: 0, last_30_days_usd: 0 }, last_active_at: null, ...more,
})

const channels: Channel[] = [
  C("web", "Web chat", null, { platform: "web", kind: null, last_seen_at: null, spend: { month_usd: 41.2, last_30_days_usd: 58.9 }, last_active_at: ago(0.3) }),
  C("c_board", "board", "Leadership", { readers: "own", writers: "own", tags: ["locked"], agent: { value: "board-desk", source: "channel" }, kept_agents: ["board-desk"] }),
  C("c_clv", "clv-models", "Projects", {
    agent: { value: "clv", source: "channel" }, spend: { month_usd: 12.4, last_30_days_usd: 19.75 }, last_active_at: ago(5),
    budget: { limit_usd: "25.00", window: "monthly", starts_at: null, ends_at: null, spent_usd: 12.4, remaining_usd: 12.6, percent_used: 49, active: true, exceeded: false },
  }),
  C("c_general", "general", null, { spend: { month_usd: 3.1, last_30_days_usd: 6.2 }, last_active_at: ago(1) }),
  C("c_mmm", "mmm", "Projects", { readers: "inside", tags: ["kept_inside"], spend: { month_usd: 28, last_30_days_usd: 30.5 }, last_active_at: ago(26),
    budget: { limit_usd: "25.00", window: "monthly", starts_at: null, ends_at: null, spent_usd: 28, remaining_usd: 0, percent_used: 100, active: true, exceeded: true } }),
  C("c_partners", "partners", null, { tags: ["shared"] }),
]
const history: Record<string, ChannelHistoryEntry[]> = {
  c_clv: [
    { at: ago(30), setting: "budget", before: null, after: { limit_usd: "25.00", window: "monthly", starts_at: null, ends_at: null }, actor: { id: "acc_jonas", name: "Jonas Weber" }, surface: "web" },
    { at: ago(80), setting: "default_agent", before: null, after: "clv", actor: { id: "acc_priya", name: "Priya Raman" }, surface: "chat" },
  ],
}

const manage = () => me()?.capabilities.includes("channels.manage") ?? false
const view = () => me()?.capabilities.includes("channels.view") ?? false
/** Members see web chat and channels their chat platform shows them, never a locked one. */
const visible = (c: Channel) => manage() || (!c.tags.includes("locked") && c.id !== "c_partners")
const find = (id: string) => channels.find((c) => c.id === id && visible(c))
const detail = (c: Channel): ChannelDetail => ({
  ...c, readers_label: READERS[c.readers], writers_label: WRITERS[c.writers],
  readers_options: Object.entries(READERS).map(([value, label]) => ({ value: value as Readers, label })),
  writers_options: Object.entries(WRITERS).map(([value, label]) => ({ value: value as Writers, label })),
  can_manage: manage(), budget_editable: manage() && c.id !== "web",
})
const stale = () => HttpResponse.json({ detail: "This changed since you opened it. Reload and try again." }, { status: 409 })
const invalid = (detail: string) => HttpResponse.json({ detail }, { status: 422 })
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

function changed(c: Channel, patch: Partial<Channel>, setting: ChannelHistoryEntry["setting"], before: unknown, after: unknown, notes: string[] = []) {
  Object.assign(c, patch)
  const who = me()!
  ;(history[c.id] ??= []).unshift({ at: new Date().toISOString(), setting, before, after, actor: { id: who.id, name: who.name }, surface: "web" })
  return HttpResponse.json({ channel: detail(c), notes })
}

export const channelsHandlers: HttpHandler[] = [
  ...["/api/channels", "/api/channels/*"].map((path) => http.all(path, async ({ request }) => {
    await wait()
    if (!view() || (request.method !== "GET" && !manage())) return forbidden()
  })),
  http.get("/api/channels", () => HttpResponse.json<ChannelList>({
    platform: "discord", default_agent: WORKSPACE, default_environment: { value: "default", source: "deployment" }, channels: channels.filter(visible),
  })),
  http.get("/api/channels/:id/history", ({ params }) => manage() ? HttpResponse.json(history[params.id as string] ?? []) : forbidden()),
  http.get("/api/channels/:id", ({ params }) => {
    const c = find(params.id as string)
    return c ? HttpResponse.json(detail(c)) : notFound()
  }),
  http.put("/api/channels/:id/default-agent", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    if (c.id === "web") return invalid("Web chat uses the workspace default agent.")
    const body = (await request.json()) as DefaultAgentChange
    const own = c.agent.source === "channel" ? c.agent.value : null
    if (body.expected !== own) return stale()
    return changed(c, { agent: body.agent ? { value: body.agent, source: "channel" } : WORKSPACE }, "default_agent", own, body.agent)
  }),
  http.post("/api/channels/:id/kept-agents", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    const body = (await request.json()) as KeptAgentChange
    if (!same(body.expected, c.kept_agents)) return stale()
    const kept = body.action === "add" ? [...new Set([...c.kept_agents, body.agent])] : c.kept_agents.filter((a) => a !== body.agent)
    return changed(c, { kept_agents: kept }, "kept_agents", c.kept_agents, kept)
  }),
  http.put("/api/channels/:id/access", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    if (c.id === "web") return invalid("Web chat has no channel rule.")
    const body = (await request.json()) as AccessChange
    if (!same(body.expected, { readers: c.readers, writers: c.writers })) return stale()
    const rule = { readers: body.readers ?? c.readers, writers: body.writers ?? c.writers }
    const tags = [...(rule.readers === "inside" ? ["kept_inside" as const] : []), ...(rule.readers === "own" ? ["locked" as const] : []), ...c.tags.filter((t) => t === "shared")]
    return changed(c, { ...rule, tags }, "access", body.expected, rule)
  }),
  http.put("/api/channels/:id/budget", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    if (c.id === "web") return invalid("Web chat has no channel budget.")
    const body = (await request.json()) as BudgetChange
    const now = c.budget && { limit_usd: c.budget.limit_usd, window: c.budget.window, starts_at: c.budget.starts_at, ends_at: c.budget.ends_at }
    if (!same(body.expected, now)) return stale()
    if (body.action === "clear") return changed(c, { budget: null }, "budget", now, null)
    const limit = body.action === "raise" ? Number(c.budget?.limit_usd ?? 0) + Number(body.amount_usd) : Number(body.limit_usd)
    if (!(limit > 0)) return invalid("Enter a budget in dollars and pick a window.")
    const spent = c.budget?.spent_usd ?? c.spend.month_usd
    const budget = {
      limit_usd: limit.toFixed(2), window: body.window ?? c.budget?.window ?? "monthly", starts_at: body.starts_at ?? null, ends_at: body.ends_at ?? null,
      spent_usd: spent, remaining_usd: Math.max(0, +(limit - spent).toFixed(2)), percent_used: Math.min(100, Math.round((spent / limit) * 100)), active: true, exceeded: spent >= limit,
    }
    return changed(c, { budget }, "budget", now, { limit_usd: budget.limit_usd, window: budget.window, starts_at: budget.starts_at, ends_at: budget.ends_at })
  }),
]

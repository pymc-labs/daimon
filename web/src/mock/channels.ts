// Channels: the workspace's channels, one channel, each section's change and its history.
import { http, HttpResponse, type HttpHandler } from "msw"

import type {
  AccessChange, BudgetChange, Channel, ChannelDefaults, ChannelDetail, ChannelHistoryEntry, ChannelList, ChannelSettings, ChannelSwitch,
  CopyRetire, DefaultAgentChange, EnvironmentChange, KeptAgentChange, PinClear, Readers, SkillChange, SwitchChange, ThreadClear,
  ToolRuleChange, WorkspaceAgentChange, Writers,
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
  C("t_ops", "ops-room", "Ops team", { platform: "teams", kind: "private", last_active_at: ago(9), spend: { month_usd: 2.4, last_30_days_usd: 4.8 } }),
]
const history: Record<string, ChannelHistoryEntry[]> = {
  c_clv: [
    { at: ago(30), setting: "budget", before: null, after: { limit_usd: "25.00", window: "monthly", starts_at: null, ends_at: null }, actor: { id: "acc_jonas", name: "Jonas Weber" }, surface: "web" },
    { at: ago(80), setting: "default_agent", before: null, after: "clv", actor: { id: "acc_priya", name: "Priya Raman" }, surface: "chat" },
  ],
}

const sw = (value: ChannelSwitch["value"], source: ChannelSwitch["source"], own: ChannelSwitch["own"] = null): ChannelSwitch =>
  ({ available: true, own, value, source })
/** A deterministic spend curve over the last `days` days that adds up to `total`. */
const curve = (total: number, days: number): { key: string; name: null; usd: number }[] => {
  const weights = Array.from({ length: days }, (_, i) => (i % 7 === 5 || i % 7 === 6 ? 0.15 : 0.6 + ((i * 37) % 11) / 10) * (i > days - 8 ? 1.4 : 1))
  const sum = weights.reduce((a, b) => a + b, 0)
  return weights.map((w, i) => ({ key: ago((days - 1 - i) * 24).slice(0, 10), name: null, usd: +((w / sum) * total).toFixed(2) }))
}
const settingsOf = (c: Channel): ChannelSettings => ({
  participation: sw("off", "deployment"),
  progress_plans: sw("on", "workspace"),
  skills: c.id === "c_clv" ? [{ skill_id: "skill_pymc", name: "pymc-modeling" }, { skill_id: "skill_brief", name: "client-brief" }] : [],
  tool_rules: { editable: true, rules: c.id === "c_clv" ? [
    { agent: "clv", tool: "web_search", rule: "ask", scope: "chat", revision: 2 },
    { agent: "clv", tool: "send_email", rule: "deny", scope: "chat_and_routines", revision: 1 },
  ] : [] },
  provider_pin: c.id === "c_clv" ? { backend: "openai", model: "gpt-5", local: 3 } : null,
  channel_copy: c.id === "c_board" ? { name: "board-desk", agent_id: "agent_board" } : null,
  threads: c.id === "c_general" ? [{ thread_id: "t_launch", kind: "progress_plans", value: "off" }, { thread_id: "t_help", kind: "handoff", value: "clv" }] : [],
  link: c.id === "web" ? null : `https://discord.com/channels/1/${c.id}`,
  teams_subscription: c.id === "t_ops" ? { status: "expired", expires_at: ago(30) } : null,
  routines: c.id === "c_clv" ? [
    { id: "r1", agent: "clv", cron: "0 9 * * 1", enabled: true, relation: "both" },
    { id: "r2", agent: "pricing-analyst", cron: "30 8 * * 1-5", enabled: false, relation: "posts_here" },
  ] : [],
  spend: {
    days: 30,
    by_day: c.spend.last_30_days_usd ? curve(c.spend.last_30_days_usd, 30) : [],
    by_agent: c.spend.last_30_days_usd ? [
      { key: "agent_1", name: c.agent.value, usd: +(c.spend.last_30_days_usd * 0.7).toFixed(2) },
      { key: "agent_2", name: "pricing-analyst", usd: +(c.spend.last_30_days_usd * 0.3).toFixed(2) },
    ] : [],
    by_person: c.spend.last_30_days_usd ? [
      { key: "u_1", name: "Priya Raman", usd: +(c.spend.last_30_days_usd * 0.55).toFixed(2) },
      { key: "u_2", name: "Jonas Weber", usd: +(c.spend.last_30_days_usd * 0.35).toFixed(2) },
      { key: null, name: null, usd: +(c.spend.last_30_days_usd * 0.1).toFixed(2) },
    ] : [],
  },
})
const settings: Record<string, ChannelSettings> = {}
const settingsFor = (c: Channel) => (settings[c.id] ??= settingsOf(c))
const defaults: ChannelDefaults = {
  platform: "discord", agent: WORKSPACE, environment: { value: "default", source: "deployment" },
  participation: sw("off", "deployment"), progress_plans: sw("on", "workspace", "on"),
}
const confirmNeeded = () => HttpResponse.json({ detail: "This channel is sealed and that sandbox has open network access. Confirm to go ahead." }, { status: 428 })

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
  http.get("/api/channels/:id/settings", ({ params }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    const s = settingsFor(c)
    return HttpResponse.json({ ...s, tool_rules: { ...s.tool_rules, editable: s.tool_rules.editable && manage() } })
  }),
  ...(["participation", "progress-plans"] as const).map((path) => http.put(`/api/channels/:id/${path}`, async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    if (c.id === "web") return invalid("Web chat has no such setting.")
    const body = (await request.json()) as SwitchChange
    const key = path === "participation" ? "participation" : "progress_plans"
    const s = settingsFor(c)
    if (body.expected !== s[key].own) return stale()
    s[key] = body.mode ? sw(body.mode, "channel", body.mode) : sw(defaults[key].value, defaults[key].source)
    return changed(c, {}, key, body.expected, body.mode)
  })),
  http.put("/api/channels/:id/environment", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    if (c.id === "web") return invalid("Web chat has no such setting.")
    const body = (await request.json()) as EnvironmentChange
    const own = c.environment.source === "channel" ? c.environment.value : null
    if (body.expected !== own) return stale()
    if (c.readers === "inside" && body.environment === "open" && !body.confirm_open_network) return confirmNeeded()
    return changed(c, { environment: body.environment ? { value: body.environment, source: "channel" } : defaults.environment }, "environment", own, body.environment)
  }),
  http.post("/api/channels/:id/skills", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    const body = (await request.json()) as SkillChange
    const s = settingsFor(c)
    if (!same([...body.expected].sort(), s.skills.map((k) => k.skill_id).sort())) return stale()
    const before = s.skills.map((k) => k.name)
    s.skills = body.action === "add" ? [...s.skills, { skill_id: `skill_${body.skill}`, name: body.skill }] : s.skills.filter((k) => k.skill_id !== body.skill)
    return changed(c, {}, "skills", before, s.skills.map((k) => k.name))
  }),
  http.put("/api/channels/:id/tool-rules", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    const body = (await request.json()) as ToolRuleChange
    const s = settingsFor(c)
    if (!s.tool_rules.editable) return invalid("Tool rules are read only while tool safety is off.")
    const prior = s.tool_rules.rules.find((r) => r.agent === body.agent && r.tool === body.tool)
    if ((prior?.revision ?? 0) !== body.expected_revision) return stale()
    const rule = { agent: body.agent, tool: body.tool, rule: body.rule, scope: body.scope ?? "chat", revision: body.expected_revision + 1 }
    s.tool_rules.rules = [...s.tool_rules.rules.filter((r) => r !== prior), rule]
    return changed(c, {}, "tool_rule", prior ?? null, rule)
  }),
  http.post("/api/channels/:id/provider-pin/clear", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    const body = (await request.json()) as PinClear
    const s = settingsFor(c)
    if (s.provider_pin?.local !== body.expected_local) return stale()
    const before = s.provider_pin
    s.provider_pin = null
    return changed(c, {}, "provider_pin", before, null)
  }),
  http.post("/api/channels/:id/channel-copy/retire", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    const body = (await request.json()) as CopyRetire
    const s = settingsFor(c)
    if (s.channel_copy?.agent_id !== body.agent_id) return stale()
    s.channel_copy = null
    return changed(c, {}, "channel_copy", body.name, null)
  }),
  http.post("/api/channels/:id/threads/clear", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    const body = (await request.json()) as ThreadClear
    const s = settingsFor(c)
    const match = s.threads.find((t) => t.thread_id === body.thread_id && t.kind === body.kind)
    if (!match) return stale()
    s.threads = s.threads.filter((t) => t !== match)
    const setting = body.kind === "handoff" ? "thread_handoff" : body.kind === "thread_rule" ? "thread_rule" : `thread_${body.kind}` as const
    return changed(c, {}, setting, { thread: match.thread_id, value: match.value }, null)
  }),
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
  http.get("/api/channel-defaults", async () => {
    await wait()
    return view() ? HttpResponse.json(defaults) : forbidden()
  }),
  http.put("/api/channel-defaults/:what", async ({ params, request }) => {
    await wait()
    if (!manage()) return forbidden()
    const what = params.what as string
    if (what === "agent") {
      const body = (await request.json()) as WorkspaceAgentChange
      if (body.expected !== (defaults.agent.source === "workspace" ? defaults.agent.value : null)) return stale()
      defaults.agent = body.agent ? { value: body.agent, source: "workspace" } : { value: "daimon", source: "deployment" }
    } else if (what === "environment") {
      const body = (await request.json()) as EnvironmentChange
      if (body.expected !== (defaults.environment.source === "workspace" ? defaults.environment.value : null)) return stale()
      defaults.environment = body.environment ? { value: body.environment, source: "workspace" } : { value: "default", source: "deployment" }
    } else if (what === "participation" || what === "progress-plans") {
      const key = what === "participation" ? "participation" : "progress_plans"
      const body = (await request.json()) as SwitchChange
      if (body.expected !== defaults[key].own) return stale()
      defaults[key] = body.mode ? sw(body.mode, "workspace", body.mode) : sw("off", "deployment")
    } else return notFound()
    return HttpResponse.json(defaults)
  }),
]

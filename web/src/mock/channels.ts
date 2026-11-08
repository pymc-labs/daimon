// Channels: the workspace's channels, one channel, each section's change and its history.
import { http, HttpResponse, type HttpHandler } from "msw"

import type {
  AccessChange, AgentRefs, ApprovalsChange, ChannelApprovals, CostFooterChange, BudgetChange, Channel, ChannelDefaults, ChannelDetail, ChannelHistoryEntry, ChannelList, ChannelSettings, ChannelSwitch,
  CopyRetire, DefaultAgentChange, EnvironmentChange, KeptAgentChange, PathNode, PinClear, Readers, SkillChange, SwitchChange, ThreadClear,
  ToolRuleChange, WorkspaceAgentChange, Writers,
} from "@/modules/channels/types"
import { roster } from "./agents"
import { forbidden, notFound, wait } from "./util"
import { me } from "./world"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const READERS: Record<Readers, string> = { any: "Anyone", inside: "Only this channel", own: "Only its own agents" }
const WRITERS: Record<Writers, string> = { any: "Anyone", own: "Only its own agents", none: "Nobody" }
const WORKSPACE = { value: "daimon", source: "workspace" } as const

/** A channel as stored: members are sent it without its budget and spend. */
type Stored = Channel & { spend: NonNullable<Channel["spend"]> }

const DISCORD_CATEGORY = (id: string, name: string): PathNode => ({ id, name, kind: "category" })
const PROJECTS = DISCORD_CATEGORY("k_projects", "Projects")
const LEADERSHIP = DISCORD_CATEGORY("k_leadership", "Leadership")
const node = (id: string, name: string, kind: PathNode["kind"] = "channel"): PathNode => ({ id, name, kind })

const C = (id: string, name: string | null, path: PathNode[], more: Partial<Stored> = {}): Stored => ({
  id, platform: "discord", name, kind: "channel", status: "active", path, parent: path.at(-1) ? { id: path.at(-1)!.id, name: path.at(-1)!.name } : null,
  last_seen_at: ago(2), agent: WORKSPACE, environment: { value: "default", source: "deployment" }, readers: "any", writers: "any", kept_agents: [], tags: [],
  budget: null, spend: { month_usd: 0, last_30_days_usd: 0 }, last_active_at: null, ...more,
})
const monthly = (spent: number, limit = 25): Channel["budget"] => ({
  limit_usd: limit.toFixed(2), window: "monthly", starts_at: null, ends_at: null, spent_usd: spent, remaining_usd: Math.max(0, +(limit - spent).toFixed(2)),
  percent_used: Math.min(100, Math.round((spent / limit) * 100)), active: true, exceeded: spent >= limit,
})
const thread = (id: string, name: string, path: PathNode[], more: Partial<Stored> = {}) => C(id, name, path, { kind: "thread", ...more })

const OPS = node("k_ops", "Ops team", "team")
const DS = node("k_ds", "Data science", "team")
const GENERAL = node("c_general", "general")
const CLV = node("c_clv", "clv-models")
const QUESTIONS = node("c_questions", "questions")

const channels: Stored[] = [
  C("web", "Web chat", [], { platform: "web", last_seen_at: null, spend: { month_usd: 41.2, last_30_days_usd: 58.9 }, last_active_at: ago(0.3) }),
  C("c_board", "board", [LEADERSHIP], { readers: "own", writers: "own", tags: ["locked"], agent: { value: "board-desk", source: "channel" }, kept_agents: ["board-desk"], last_active_at: ago(70) }),
  C("c_clv", "clv-models", [PROJECTS], {
    agent: { value: "clv", source: "channel" }, spend: { month_usd: 12.4, last_30_days_usd: 19.75 }, last_active_at: ago(5), budget: monthly(12.4),
  }),
  thread("t_q3", "Q3 holdout check", [PROJECTS, CLV], { agent: { value: "pricing-analyst", source: "channel" }, spend: { month_usd: 1.8, last_30_days_usd: 1.8 }, last_active_at: ago(7) }),
  thread("t_priors", "Priors review", [PROJECTS, CLV], { last_active_at: ago(400) }),
  C("c_mmm", "mmm", [PROJECTS], { readers: "inside", tags: ["kept_inside"], spend: { month_usd: 28, last_30_days_usd: 30.5 }, last_active_at: ago(26), budget: monthly(28) }),
  C("c_questions", "questions", [PROJECTS], { last_active_at: ago(12) }),
  C("p_roas", "How to read ROAS?", [PROJECTS, QUESTIONS], { kind: "forum_post", last_active_at: ago(12) }),
  C("c_general", "general", [], { spend: { month_usd: 3.1, last_30_days_usd: 6.2 }, last_active_at: ago(1) }),
  thread("t_launch", "Launch", [GENERAL], { last_active_at: ago(3) }),
  thread("t_help", "Help", [GENERAL], { last_active_at: ago(30) }),
  thread("t_weekly", "Weekly sync", [node("c_opsnotes", "ops-notes")], { last_active_at: ago(50) }),
  C("c_partners", "partners", [], { tags: ["shared"] }),
  C("t_ops", "ops-room", [OPS], { platform: "teams", last_active_at: ago(9), spend: { month_usd: 2.4, last_30_days_usd: 4.8 } }),
  thread("t_incident", "Incident 14", [OPS, node("t_ops", "ops-room")], { platform: "teams", last_active_at: ago(10) }),
  C("t_general", "General", [OPS], { platform: "teams", last_active_at: ago(40) }),
  C("t_models", "models", [DS], { platform: "teams", last_active_at: ago(60) }),
  C("s_pricing", "pricing", [], { platform: "slack", agent: { value: "pricing-analyst", source: "channel" }, last_active_at: ago(4), spend: { month_usd: 6.2, last_30_days_usd: 9.4 } }),
  thread("s_promo", "Promo test", [node("s_pricing", "pricing")], { platform: "slack", last_active_at: ago(6) }),
  // Channels the platform no longer has, and direct messages: kept for their settings, never listed.
  C("g_old1", "old-launch", [], { status: "gone" }),
  C("g_old2", "q1-planning", [], { status: "gone", budget: monthly(4, 10) }),
  C("d_dm1", null, [], { status: "direct", kind: "channel" }),
  C("s_gone", "retired-team", [], { platform: "slack", status: "gone" }),
]
/** Agent keys as channels store them, and the roster agent each names. A key not found here is an agent that no longer exists. */
const KEYS: Record<string, string> = { daimon: "ag_daimon", clv: "ag_mmm", "pricing-analyst": "ag_pricing", "board-desk": "ag_sales" }
function refOf(key: string): AgentRefs[string] {
  const recs = roster.all()
  const rec = recs.find((r) => r.id === KEYS[key]) ?? recs.find((r) => r.versions.at(-1)!.config.name === key)
  const cfg = rec?.versions.at(-1)!.config
  return rec && cfg ? { id: rec.id, name: cfg.name, avatar: cfg.avatar } : { id: null, name: null, avatar: null }
}
const refs = (keys: Iterable<string | null | undefined>): AgentRefs =>
  Object.fromEntries([...new Set([...keys].filter((k): k is string => !!k))].map((k) => [k, refOf(k)]))
const keysOf = (c: Stored) => [c.agent.value, ...c.kept_agents]
const history: Record<string, ChannelHistoryEntry[]> = {
  c_clv: [
    { at: ago(30), setting: "budget", before: null, after: { limit_usd: "25.00", window: "monthly", starts_at: null, ends_at: null }, actor: { id: "acc_jonas", name: "Jonas Weber" }, surface: "web" },
    { at: ago(80), setting: "default_agent", before: null, after: "clv", actor: { id: "acc_priya", name: "Priya Raman" }, surface: "chat" },
  ],
}
const historyKeys = (id: string) => (history[id] ?? []).flatMap((e) => [e.before, e.after].flatMap((v) => (typeof v === "string" ? [v] : Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [])))

const sw = (value: ChannelSwitch["value"], source: ChannelSwitch["source"], own: ChannelSwitch["own"] = null): ChannelSwitch =>
  ({ available: true, own, value, source })
/** A deterministic spend curve over the last `days` days that adds up to `total`. */
const curve = (total: number, days: number): { key: string; name: null; usd: number }[] => {
  const weights = Array.from({ length: days }, (_, i) => (i % 7 === 5 || i % 7 === 6 ? 0.15 : 0.6 + ((i * 37) % 11) / 10) * (i > days - 8 ? 1.4 : 1))
  const sum = weights.reduce((a, b) => a + b, 0)
  return weights.map((w, i) => ({ key: ago((days - 1 - i) * 24).slice(0, 10), name: null, usd: +((w / sum) * total).toFixed(2) }))
}
const ap = (value: ChannelApprovals["value"], source: ChannelApprovals["source"], own: ChannelApprovals["own"] = null): ChannelApprovals =>
  ({ available: true, own, value, source, active: false, editable: true })
const settingsOf = (c: Stored): ChannelSettings => ({
  participation: sw("off", "deployment"),
  progress_plans: sw("on", "workspace"),
  approvals: ap("on", "default"),
  skills: c.id === "c_clv" ? [{ skill_id: "data-validation", name: "Data validation" }, { skill_id: "exploratory-data-analysis", name: "Exploratory analysis" }] : [],
  tool_rules: { editable: true, rules: c.id === "c_clv" ? [
    { agent: "clv", tool: "search/web_search", tool_label: "Web search", server_name: "Search", rule: "ask", scope: "chat", revision: 2 },
    { agent: "clv", tool: "mail/send_email", tool_label: "Send email", server_name: "Mail", rule: "deny", scope: "chat_and_routines", revision: 1 },
    { agent: "retired-analyst", tool: "bigquery/run_query", tool_label: "Run query", server_name: "Bigquery", rule: "allow", scope: "chat", revision: 1 },
  ] : [] },
  provider_pin: c.id === "c_clv" ? { backend: "openai", model: "gpt-5.5", model_label: null, local: 3 } : null,
  channel_copy: c.id === "c_board" ? { name: "board-desk", agent_id: "agent_board" } : null,
  threads: c.id === "c_general" ? [
    { thread_id: "t_launch", name: "Launch", kind: "progress_plans", value: "off" }, { thread_id: "t_help", name: "Help", kind: "handoff", value: "clv" },
  ] : c.id === "c_clv" ? [{ thread_id: "t_q3", name: "Q3 holdout check", kind: "handoff", value: "retired-analyst" }, { thread_id: "t_gone", name: null, kind: "participation", value: "on" }] : [],
  link: c.id === "web" ? null : `https://${c.platform}.com/channels/1/${c.id}`,
  teams_subscription: c.id === "t_ops" ? { status: "expired", expires_at: ago(30) } : null,
  routines: c.id === "c_clv" ? [
    { id: "r1", title: "Monday readout", agent: "clv", cron: "0 9 * * 1", enabled: true, relation: "both" },
    { id: "r2", title: "Weekday pricing check", agent: "pricing-analyst", cron: "30 8 * * 1-5", enabled: false, relation: "posts_here" },
  ] : [],
  spend: {
    days: 30,
    by_day: c.spend.last_30_days_usd ? curve(c.spend.last_30_days_usd, 30) : [],
    by_agent: c.spend.last_30_days_usd ? [
      { key: "ag_mmm", name: "MMM Analyst", usd: +(c.spend.last_30_days_usd * 0.7).toFixed(2) },
      { key: "ag_pricing", name: "Pricing Analyst", usd: +(c.spend.last_30_days_usd * 0.3).toFixed(2) },
    ] : [],
    by_person: c.spend.last_30_days_usd ? [
      { key: "u_1", name: "Priya Raman", usd: +(c.spend.last_30_days_usd * 0.55).toFixed(2) },
      { key: "u_2", name: "Jonas Weber", usd: +(c.spend.last_30_days_usd * 0.35).toFixed(2) },
      { key: null, name: null, usd: +(c.spend.last_30_days_usd * 0.1).toFixed(2) },
    ] : [],
  },
  agents: {},
})
const settings: Record<string, ChannelSettings> = {}
const settingsFor = (c: Stored) => (settings[c.id] ??= settingsOf(c))
const withAgents = (s: ChannelSettings): ChannelSettings => ({
  ...s,
  agents: refs([...s.tool_rules.rules.map((r) => r.agent), ...s.routines.map((r) => r.agent), ...s.threads.filter((t) => t.kind === "handoff").map((t) => t.value)]),
})
const defaults: ChannelDefaults = {
  platform: "discord", agent: WORKSPACE, environment: { value: "default", source: "deployment" },
  participation: sw("off", "deployment"), progress_plans: sw("on", "workspace", "on"),
  approvals: ap("on", "default"), cost_footer: { shown: true, editable: true },
}
const confirmNeeded = () => HttpResponse.json({ detail: "This channel is sealed and that sandbox has open network access. Confirm to go ahead." }, { status: 428 })

const manage = () => me()?.capabilities.includes("channels.manage") ?? false
const view = () => me()?.capabilities.includes("channels.view") ?? false
/** Members see web chat and channels their chat platform shows them, never a locked one. */
const visible = (c: Stored) => manage() || (!c.tags.includes("locked") && c.id !== "c_partners")
/** The directory leaves out channels the platform no longer has and direct messages; a direct id still reads. */
const listed = (c: Stored) => c.status === "active" && visible(c)
const find = (id: string) => channels.find((c) => c.id === id && visible(c))
const figures = (c: Stored): Channel => (manage() ? c : { ...c, budget: null, spend: null })
const detail = (c: Stored): ChannelDetail => ({
  ...figures(c), readers_label: READERS[c.readers], writers_label: WRITERS[c.writers],
  readers_options: Object.entries(READERS).map(([value, label]) => ({ value: value as Readers, label })),
  writers_options: Object.entries(WRITERS).map(([value, label]) => ({ value: value as Writers, label })),
  can_manage: manage(), budget_editable: manage() && c.id !== "web", agents: refs(keysOf(c)),
})
const stale = () => HttpResponse.json({ detail: "This changed since you opened it. Reload and try again." }, { status: 409 })
const invalid = (detail: string) => HttpResponse.json({ detail }, { status: 422 })
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

function changed(c: Stored, patch: Partial<Stored>, setting: ChannelHistoryEntry["setting"], before: unknown, after: unknown, notes: string[] = []) {
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
    platform: "discord", default_agent: WORKSPACE, default_environment: { value: "default", source: "deployment" },
    channels: channels.filter(listed).map(figures), agents: refs(["daimon", ...channels.filter(listed).flatMap(keysOf)]),
  })),
  http.get("/api/channels/:id/history", ({ params }) => manage()
    ? HttpResponse.json({ entries: history[params.id as string] ?? [], agents: refs(historyKeys(params.id as string)) })
    : forbidden()),
  http.get("/api/channels/:id/settings", ({ params }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    const s = settingsFor(c)
    return HttpResponse.json({ ...withAgents(s), spend: manage() ? s.spend : null, tool_rules: { ...s.tool_rules, editable: s.tool_rules.editable && manage() } })
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
  http.put("/api/channels/:id/approvals", async ({ params, request }) => {
    const c = find(params.id as string)
    if (!c) return notFound()
    if (c.id === "web") return invalid("Web chat has no such setting.")
    const body = (await request.json()) as ApprovalsChange
    const s = settingsFor(c)
    if (body.expected !== s.approvals.own) return stale()
    s.approvals = body.mode ? ap(body.mode, "channel", body.mode) : ap(defaults.approvals.value, defaults.approvals.own ? "server" : "default")
    return changed(c, {}, "approvals", body.expected ?? "inherit", body.mode ?? "inherit")
  }),
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
    s.skills = body.action === "add" ? [...s.skills, { skill_id: body.skill, name: body.skill.replaceAll("-", " ").replace(/^./, (x: string) => x.toUpperCase()) }] : s.skills.filter((k) => k.skill_id !== body.skill)
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
    const words = (k: string) => { const w = k.replaceAll(/[_-]+/g, " "); return w.charAt(0).toUpperCase() + w.slice(1) }
    const [server = "", tool = ""] = body.tool.includes("/") ? body.tool.split("/") : ["", body.tool]
    const rule = { agent: body.agent, tool: body.tool, tool_label: words(tool), server_name: server ? words(server) : "", rule: body.rule, scope: body.scope ?? "chat", revision: body.expected_revision + 1 }
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
    } else if (what === "approvals") {
      const body = (await request.json()) as ApprovalsChange
      if (body.expected !== defaults.approvals.own) return stale()
      defaults.approvals = body.mode ? ap(body.mode, "server", body.mode) : ap("on", "default")
    } else if (what === "cost-footer") {
      const body = (await request.json()) as CostFooterChange
      if (body.expected !== defaults.cost_footer.shown) return stale()
      defaults.cost_footer = { ...defaults.cost_footer, shown: body.shown }
    } else return notFound()
    return HttpResponse.json(defaults)
  }),
]

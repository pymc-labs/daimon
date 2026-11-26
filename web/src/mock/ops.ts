// Ops: the admin overview and grouped issues, built relative to now on every load.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { Issue, IssueAction, IssueDetail, IssueEvent, IssueState, OpsArea, OpsOverview, OpsPeriod, OpsSeverity } from "@/modules/admin/types"
import { forbidden, notFound, paged, wait } from "./util"
import { me } from "./world"

const H = 3_600_000
const iso = (msAgo: number) => new Date(Date.now() - msAgo).toISOString()
const HOURS: Record<OpsPeriod, number> = { "24h": 24, "7d": 168, "30d": 720 }
const AREAS: Record<OpsArea, string> = {
  turn: "Answers", routine: "Routines", bridge: "Bridged tools", dm: "Direct messages", watch: "Watches", delivery: "Delivery", wake: "Follow-ups",
}

type Seed = Omit<Issue, "first_seen" | "last_seen" | "buckets" | "new" | "active" | "people" | "state" | "regressed" | "state_by" | "state_at"> & {
  first: number; last: number; people: [string, string][]; agent?: [string, string]; where: string; platform: string | null; note?: string
}
const MAYA: [string, string] = ["p_maya", "Maya Lindqvist"], AIKO: [string, string] = ["p_aiko", "Aiko Tanaka"], TOMAS: [string, string] = ["p_tomas", "Tomás Okafor"]
const PRIYA: [string, string] = ["p_priya", "Priya Raman"], OWEN: [string, string] = ["p_owen", "Owen Brooks"], RAFAEL: [string, string] = ["p_rafael", "Rafael Costa"]
const INES: [string, string] = ["p_ines", "Inês Duarte"], JONAS: [string, string] = ["p_jonas", "Jonas Weber"]
const MMM: [string, string] = ["ag_mmm", "MMM Analyst"], PRICING: [string, string] = ["ag_pricing", "Pricing Analyst"]

const s = (fingerprint: string, code: string, title: string, area: OpsArea, severity: OpsSeverity, subject: Issue["subject"], reason: string | null, reason_label: string | null,
  events: number, first: number, last: number, people: [string, string][], rest: Partial<Seed> = {}): Seed =>
  ({ fingerprint, code, title, area, severity, subject, reason, reason_label, events, first, last, people, where: "#mmm-larkspur", platform: "discord", ...rest })

const SEEDS: Seed[] = [
  s("f_turn_mmm", "turn.failed", "Answer failed", "turn", "error", { kind: "agent", id: "ag_mmm", name: "MMM Analyst" }, "timeout", "Timed out", 31, 3, 0.02,
    [AIKO, TOMAS, RAFAEL, MAYA, OWEN, PRIYA, INES, JONAS, ["p_lena", "Lena Fischer"]], { agent: MMM }),
  s("f_relay_internal", "bridge.relay_failed", "Bridged tools unavailable", "bridge", "error", { kind: "server", id: "internal-tools", name: "internal-tools" }, "502", "Server error", 41, 50, 0.015,
    [AIKO, TOMAS, MAYA, OWEN, PRIYA, INES], { where: "Coding tools", platform: null }),
  s("f_fire_r2", "routine.fire_failed", "Routine failed to run", "routine", "error", { kind: "routine", id: "r2", name: "Overnight price test" }, "timeout", "Timed out", 3, 10, 0.07,
    [PRIYA], { agent: PRICING, where: "#pricing" }),
  s("f_dm_reply", "dm.failed", "Direct message not answered", "dm", "error", { kind: "platform", id: "discord", name: "Discord" }, "reply", "Reply failed", 4, 30, 0.4,
    [OWEN, INES, TOMAS], { where: "Direct message" }),
  s("f_watch_inbox", "watch.poll_failed", "Watch check failed", "watch", "warning", { kind: "watch", id: "w1", name: "Client inbox" }, "SourceBusy", "Source busy", 5, 100, 0.2,
    [MAYA], { where: "Gmail", platform: null }),
  s("f_delivery_r1", "routine.delivery_failed", "Routine result not delivered", "delivery", "error", { kind: "routine", id: "r1", name: "Weekly channel readout" }, "post_failed", "Post failed", 2, 1, 0.97,
    [MAYA], { agent: MMM }),
  s("f_drive", "connector.reconnect_needed", "Connector needs reconnecting", "bridge", "warning", { kind: "connector", id: "google-drive", name: "Google Drive" }, "expired", "Sign-in expired", 5, 144, 2,
    [RAFAEL, PRIYA], { where: "Web", platform: null }),
  s("f_shed_discord", "turn.shed", "Turned away while busy", "turn", "warning", { kind: "platform", id: "discord", name: "Discord" }, "busy", "Busy", 2, 4, 3, [OWEN, AIKO]),
  s("f_wake_fly", "wake.failed", "Follow-up did not run", "wake", "error", { kind: "wake", id: "fly_job", name: "Model run" }, "abandoned", "Abandoned", 2, 40, 5, [TOMAS], { agent: MMM }),
  s("f_output_slack", "delivery.output_failed", "File or table not delivered", "delivery", "warning", { kind: "platform", id: "slack", name: "Slack" }, "upload_failed", "Upload failed", 1, 30, 8,
    [INES], { platform: "slack", where: "#pricing-desk" }),
  s("f_watch_alerts", "watch.paused_failing", "Watch paused after repeated errors", "watch", "error", { kind: "watch", id: "w2", name: "Pricing alerts" }, "CredentialMissing", "Credential missing", 1, 72, 20,
    [PRIYA], { where: "HubSpot", platform: null }),
  s("f_goal_r1", "routine.goal_unmet", "Routine could not finish its task", "routine", "warning", { kind: "routine", id: "r1", name: "Weekly channel readout" }, "blocked_access", "Blocked access", 1, 50, 50,
    [MAYA], { agent: MMM, note: "The Larkspur spend sheet is no longer shared with this agent." }),
  s("f_turn_pricing", "turn.failed", "Answer failed", "turn", "error", { kind: "agent", id: "ag_pricing", name: "Pricing Analyst" }, "credit_cap", "Credit limit reached", 6, 120, 70,
    [OWEN, INES], { agent: PRICING, where: "#pricing" }),
  s("f_tool_errors", "bridge.tool_errors", "Bridged tool returned errors", "bridge", "warning", { kind: "server", id: "internal-tools", name: "internal-tools" }, null, null, 12, 200, 30,
    [AIKO, TOMAS, MAYA], { where: "Coding tools", platform: null }),
  s("f_hubspot", "connector.call_failed", "Connector call failed", "bridge", "warning", { kind: "connector", id: "hubspot", name: "HubSpot" }, "rate_limited", "Rate limited", 9, 400, 96,
    [JONAS, OWEN], { where: "Web", platform: null }),
]

/** The issue's events spread over the buckets it was seen in, rising toward its last sighting. */
function bucketsOf(seed: Seed, period: OpsPeriod): number[] {
  const hours = HOURS[period], n = period === "24h" ? 24 : hours / 24, span = hours / n
  // Bucket i, oldest first, covers [hours - (i + 1) * span, hours - i * span] hours ago.
  const w = Array.from({ length: n }, (_, i) =>
    seed.last <= hours - i * span && seed.first >= hours - (i + 1) * span ? (i + 1) ** 2 * (1 + ((i * 7 + seed.events) % 3)) : 0)
  const sum = w.reduce((a, b) => a + b, 0) || 1
  const share = seed.first <= hours ? seed.events : Math.max(1, Math.round((seed.events * (hours - seed.last)) / (seed.first - seed.last)))
  const out = w.map((x) => Math.floor((x / sum) * share))
  let left = share - out.reduce((a, b) => a + b, 0)
  for (let i = n - 1; left > 0 && i >= 0; i--) if (w[i]) { out[i]!++; left-- }
  return out
}

// Resolve and mute, kept for this load: one resolved, one muted, one that came back after a resolve.
type Held = { state: Exclude<IssueState, "open">; by: { id: string; name: string }; at: string }
const held = new Map<string, Held>([
  ["f_turn_pricing", { state: "resolved", by: { id: "p_jonas", name: "Jonas Weber" }, at: iso(60 * H) }],
  ["f_hubspot", { state: "muted", by: { id: "p_maya", name: "Maya Lindqvist" }, at: iso(90 * H) }],
])
const regressed = new Set(["f_drive"])

function issueOf(seed: Seed, period: OpsPeriod): Issue {
  const { first, last, people, agent: _a, where: _w, platform: _p, note: _n, ...rest } = seed
  const h = held.get(seed.fingerprint)
  const buckets = bucketsOf(seed, period)
  return {
    ...rest, events: buckets.reduce((x, y) => x + y, 0), people: people.length, first_seen: iso(first * H), last_seen: iso(last * H), buckets, new: first <= 24, active: last <= 24,
    state: h?.state ?? "open", regressed: !h && regressed.has(seed.fingerprint), state_by: h?.by ?? null, state_at: h?.at ?? null,
  }
}

const rank = (a: Issue, b: Issue) => (a.severity === b.severity ? b.last_seen.localeCompare(a.last_seen) : a.severity === "error" ? -1 : 1)

export function issuesOf(q: URLSearchParams): Issue[] {
  const period = (q.get("period") || "24h") as OpsPeriod
  const term = (q.get("q") ?? "").toLowerCase()
  const status = q.get("status") || "active"
  const rows = SEEDS.filter((x) => x.last <= HOURS[period]).map((x) => issueOf(x, period)).filter((i) =>
    (status === "active" || status === "quiet" ? i.state === "open" && i.active === (status === "active") : i.state === status) && (!q.get("area") || i.area === q.get("area")) && (!q.get("severity") || i.severity === q.get("severity"))
    && (!q.get("agent") || SEEDS.find((x) => x.fingerprint === i.fingerprint)!.agent?.[0] === q.get("agent"))
    && (!term || `${i.title} ${i.subject?.name ?? ""} ${i.reason_label ?? ""}`.toLowerCase().includes(term)))
  return q.get("sort") === "events" ? rows.sort((a, b) => b.events - a.events) : rows.sort((a, b) => b.last_seen.localeCompare(a.last_seen))
}

function eventsOf(seed: Seed): IssueEvent[] {
  const n = Math.min(seed.events, 40)
  const step = n > 1 ? (seed.first - seed.last) / (n - 1) : 0
  const rows: IssueEvent[] = []
  for (let i = 0, left = seed.events; i < n && left > 0; i++) {
    const count = i < n - 1 ? Math.max(1, Math.min(left - (n - 1 - i), i % 4 === 1 ? 2 : 1)) : left
    left -= count
    const [id, name] = seed.people[i % seed.people.length]!
    rows.push({
      at: iso((seed.last + step * i) * H), count, person: { id, name }, platform: seed.platform, where_label: seed.where, reason_label: seed.reason_label,
      thread_url: seed.platform === "discord" && seed.where.startsWith("#") ? `https://discord.com/channels/100/200/${300 + i}` : null, note: seed.note ?? null,
    })
  }
  return rows
}

const PERIOD_SEED: Record<OpsPeriod, { n: number; perBucket: number }> = { "24h": { n: 24, perBucket: 52 }, "7d": { n: 7, perBucket: 1180 }, "30d": { n: 30, perBucket: 1120 } }

export function overviewOf(period: OpsPeriod): OpsOverview {
  const { n, perBucket } = PERIOD_SEED[period]
  const span = HOURS[period] / n
  const series = Array.from({ length: n }, (_, i) => {
    const at = new Date(Date.now() - (n - i) * span * H)
    const wave = period === "24h" ? 0.25 + 0.9 * (1 + Math.sin(((at.getHours() - 9) / 24) * 2 * Math.PI)) / 2 + ((i * 7) % 5) / 25 : 0.7 + ((i * 5) % 7) / 10
    const total = Math.round(perBucket * wave)
    const failed = Math.round(total * (i >= n - 3 ? 0.045 : 0.015 + (i % 3) * 0.004))
    return { at: at.toISOString(), answered: total - failed, failed }
  })
  const failed = series.reduce((a, b) => a + b.failed, 0)
  const answers = series.reduce((a, b) => a + b.answered + b.failed, 0)
  const seen = SEEDS.filter((x) => x.last <= HOURS[period])
  const by_area = (Object.keys(AREAS) as OpsArea[]).map((area) => ({
    area, label: AREAS[area], events: seen.filter((x) => x.area === area).reduce((a, x) => a + bucketsOf(x, period).reduce((p, q) => p + q, 0), 0),
  }))
  const active = SEEDS.map((x) => issueOf(x, period)).filter((i) => i.active && i.state === "open")
  return {
    updated_at: new Date().toISOString(),
    status: active.some((i) => i.severity === "error" && Date.now() - Date.parse(i.last_seen) < H) ? "degraded" : "healthy",
    answers, failed, failed_rate: answers ? failed / answers : 0,
    dm_failures: seen.filter((x) => x.area === "dm" && !held.has(x.fingerprint)).reduce((a, x) => a + bucketsOf(x, period).reduce((p, q) => p + q, 0), 0),
    routines_failing: 1, routines_total: 3, balance_usd: 1240.5, runway_days: 38,
    series, by_area, attention: active.sort(rank).slice(0, 8),
    answers_facts: { p50_ms: 14_200, p95_ms: 71_400, busy: period === "24h" ? 2 : 9, recovered: period === "24h" ? 9 : 47, cost_per_answer_usd: 0.21 },
    workspace: { agents: 5, people: 11, channels: 9, routines_on: 2, routines_total: 3, watches_on: 3, watches_total: 4, watches_failing: 2, funding_mode: "prepaid" },
  }
}

export function issueDetailOf(fingerprint: string, period: OpsPeriod = "24h"): IssueDetail | null {
  const seed = SEEDS.find((x) => x.fingerprint === fingerprint)
  if (!seed) return null
  return {
    ...issueOf(seed, period), events: seed.events, people: seed.people.map(([id, name]) => ({ id, name })),
    agent: seed.agent ? { id: seed.agent[0], name: seed.agent[1] } : null, release: "2026.11.20",
  }
}

const viewer = () => me()?.capabilities.includes("audit.view")

function setState(fingerprint: string, action: IssueAction) {
  const m = me()!
  if (action === "reopen") held.delete(fingerprint)
  else held.set(fingerprint, { state: action === "resolve" ? "resolved" : "muted", by: { id: m.id, name: m.name }, at: new Date().toISOString() })
  regressed.delete(fingerprint)
}

export const opsHandlers: HttpHandler[] = [
  http.get("/api/admin/overview", async ({ request }) => {
    await wait()
    if (!viewer()) return forbidden()
    return HttpResponse.json(overviewOf((new URL(request.url).searchParams.get("period") || "24h") as OpsPeriod))
  }),
  http.get("/api/admin/issues", async ({ request }) => {
    await wait()
    return viewer() ? paged(request, issuesOf(new URL(request.url).searchParams), 50) : forbidden()
  }),
  http.get("/api/admin/issues/:fingerprint", async ({ params }) => {
    await wait()
    if (!viewer()) return forbidden()
    const found = issueDetailOf(String(params.fingerprint))
    return found ? HttpResponse.json(found) : notFound()
  }),
  http.post("/api/admin/issues/:fingerprint/state", async ({ params, request }) => {
    await wait()
    if (!me()?.capabilities.includes("workspace.manage")) return forbidden()
    const seed = SEEDS.find((x) => x.fingerprint === params.fingerprint)
    if (!seed) return notFound()
    setState(seed.fingerprint, ((await request.json()) as { action: IssueAction }).action)
    return HttpResponse.json(issueOf(seed, "24h"))
  }),
  http.get("/api/admin/issues/:fingerprint/events", async ({ params, request }) => {
    await wait()
    if (!viewer()) return forbidden()
    const seed = SEEDS.find((x) => x.fingerprint === params.fingerprint)
    return seed ? paged(request, eventsOf(seed), 20) : notFound()
  }),
]

// Credits: tenant balance, spend and allowances for Fernhill Labs.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { Allowance, LedgerEntry, Mine, SpendRow, Turn } from "@/modules/credits/types"
import { forbidden, unauthorized, wait } from "./util"
import { agent, AGENTS, me, SESSIONS } from "./world"
import { face } from "./face"

let redeemed = false
const day = (d: number) => new Date(Date.UTC(2026, 9, d)).toISOString()
const midnight = (back: number) => {
  const t = new Date()
  return new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate() - back)).toISOString()
}
const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()

// Deterministic daily spend for the last 30 days, today last: a weekday rhythm with a slow rise.
const DAILY = Array.from({ length: 30 }, (_, i) => {
  const weekday = new Date(midnight(29 - i)).getUTCDay()
  const rhythm = weekday === 0 || weekday === 6 ? 0.35 : 1
  const wobble = 1 + 0.25 * Math.sin(i * 1.7) + 0.1 * Math.cos(i * 0.6)
  return { day: midnight(29 - i), usd: Math.round((13 + i * 0.3) * rhythm * wobble * 100) / 100 }
})

let balance = 1240.5
const ledger: LedgerEntry[] = [
  { id: "l4", at: day(21), kind: "topup", amount_usd: 500, by: "Maya Lindqvist", note: "Card ending 4242" },
  { id: "l3", at: day(2), kind: "promo", amount_usd: 50, by: "Jonas Weber", note: "FERNHILL50" },
  { id: "l2", at: day(1), kind: "topup", amount_usd: 1000, by: "Maya Lindqvist", note: "Card ending 4242" },
  { id: "l1", at: new Date(Date.UTC(2026, 8, 12)).toISOString(), kind: "adjustment", amount_usd: 25, by: "Support", note: "Outage credit" },
]

const PERSON: SpendRow[] = [
  { id: "p_maya", name: "Maya Lindqvist", turns: 212, usd: 36.8 },
  { id: "p_priya", name: "Priya Raman", turns: 168, usd: 27.6 },
  { id: "p_tomas", name: "Tomás Okafor", turns: 140, usd: 24.15 },
  { id: "p_jonas", name: "Jonas Weber", turns: 97, usd: 18.25 },
  { id: "p_aiko", name: "Aiko Tanaka", turns: 88, usd: 14.9 },
  { id: "p_rafael", name: "Rafael Costa", turns: 61, usd: 9.4 },
  { id: "p_owen", name: "Owen Brooks", turns: 33, usd: 4.85 },
  { id: "p_jordan", name: "Jordan Ellis", kind: "Visitor", turns: 9, usd: 1.15 },
  { id: "none", name: "No person", turns: 4, usd: 0.6 },
]
const AGENT_ROWS: SpendRow[] = [
  { id: "ag_mmm", name: "MMM Analyst", avatar: agent("ag_mmm").avatar, turns: 241, usd: 140.2 },
  { id: "ag_daimon", name: "Daimon", avatar: agent("ag_daimon").avatar, turns: 402, usd: 98.4 },
  { id: "ag_pricing", name: "Pricing Analyst", avatar: agent("ag_pricing").avatar, turns: 133, usd: 61.3 },
  { id: "ag_northwind", name: "Northwind Support", avatar: face(39), turns: 58, usd: 56.2 },
  { id: "ag_review", name: "Code Reviewer", avatar: agent("ag_review").avatar, turns: 77, usd: 44.1 },
  { id: "ag_sales", name: "Sales Desk", avatar: agent("ag_sales").avatar, turns: 91, usd: 12.6 },
]
const CHANNEL: SpendRow[] = [
  { id: "c1", name: "#mmm-larkspur", kind: "discord", turns: 190, usd: 118.4 },
  { id: "c2", name: "Direct messages", kind: "discord", turns: 301, usd: 92.3 },
  { id: "c3", name: "#northwind-support", kind: "discord", turns: 58, usd: 56.2 },
  { id: "c4", name: "#pricing", kind: "discord", turns: 96, usd: 47.75 },
  { id: "c5", name: "Web", kind: "web", turns: 211, usd: 41.6 },
  { id: "c6", name: "Routines", kind: "routine", turns: 146, usd: 33.9 },
  { id: "c7", name: "#eng-review", kind: "discord", turns: 40, usd: 22.65 },
  { id: "none", name: "No channel", turns: 6, usd: 1.1 },
]

const MEMBERS = 10
const allowances: Allowance[] = [
  { id: "al1", subject: { kind: "group", id: "everyone", name: "Everyone" }, agent: null, amount_usd: 40, used_usd: 0, members: MEMBERS, window: "month", expires_at: null, cap: false },
  { id: "al0", subject: { kind: "group", id: "everyone", name: "Everyone" }, agent: null, amount_usd: 150, used_usd: 0, members: MEMBERS, window: "month", expires_at: null, cap: true },
  { id: "al2", subject: { kind: "group", id: "Leadership", name: "Leadership" }, agent: null, amount_usd: 100, used_usd: 0, members: 4, window: "month", expires_at: null, cap: false },
  { id: "al3", subject: { kind: "group", id: "MMM", name: "MMM" }, agent: agent("ag_mmm"), amount_usd: 60, used_usd: 0, members: 3, window: "month", expires_at: null, cap: false },
  { id: "al4", subject: { kind: "person", id: "p_maya", name: "Maya Lindqvist" }, agent: null, amount_usd: 100, used_usd: 36.8, window: "month", expires_at: null, cap: false },
  { id: "al5", subject: { kind: "invite", id: "inv_demo", name: "Jordan Ellis" }, agent: agent("ag_pricing"), amount_usd: 5, used_usd: 1.15, window: "total", expires_at: new Date(Date.now() + 14 * 86_400_000).toISOString(), cap: false },
]

const MY_TURNS = (uid: string): Turn[] => (SESSIONS[uid] ?? SESSIONS.p_maya!).flatMap((s, i) => [0, 1].map((k) => ({
  id: `${s.id}-${k}`, at: ago(i * 20 + k * 3 + 0.2), agent: s.agent, title: s.title, platform: k ? "discord" : "web",
  usd: Math.round((0.18 + ((i * 7 + k * 3) % 9) * 0.21) * 100) / 100,
}))).slice(0, 12)

const PERIOD_SCALE: Record<string, number> = { "30d": 1, this: 0.4, last: 1.18 }
const scale = (rows: SpendRow[], f: number) => rows.map((r) => ({ ...r, usd: Math.round(r.usd * f * 100) / 100, turns: Math.round(r.turns * f) }))

const admin = () => me()?.capabilities.includes("credits.manage")

export const creditsHandlers: HttpHandler[] = [
  http.get("/api/credits/meter", async () => {
    await wait(60)
    const m = me()
    return m ? HttpResponse.json(m.credit) : unauthorized()
  }),
  http.get("/api/credits/mine", async ({ request }) => {
    await wait()
    const m = me()
    if (!m) return unauthorized()
    const turns = MY_TURNS(m.id)
    const spent = (m.credit ? m.credit.total_usd - m.credit.left_usd : 0) * PERIOD_SCALE[new URL(request.url).searchParams.get("period") ?? "30d"]!
    const raw = AGENTS.map((a) => ({ a, n: turns.filter((x) => x.agent.id === a.id).reduce((s, x) => s + x.usd, 0) })).filter((r) => r.n > 0)
    const sum = raw.reduce((s, r) => s + r.n, 0)
    const by = raw.map(({ a, n }) => ({ id: a.id, name: a.name, avatar: a.avatar, turns: Math.round((n / sum) * 60), usd: Math.round((n / sum) * spent * 100) / 100 }))
      .sort((a, b) => b.usd - a.usd)
    const body: Mine = { by_agent: by, turns, spent_usd: spent, turn_count: by.reduce((s, r) => s + r.turns, 0) }
    return HttpResponse.json(body)
  }),
  http.get("/api/credits/overview", async () => {
    await wait()
    if (!admin()) return forbidden()
    const spent = DAILY.reduce((s, d) => s + d.usd, 0)
    const month = DAILY.filter((d) => d.day.slice(0, 7) === midnight(0).slice(0, 7)).reduce((s, d) => s + d.usd, 0)
    const now = new Date()
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()
    return HttpResponse.json({
      balance_usd: balance, spent_usd: spent, turns: 1002, spent_month_usd: month, projected_month_usd: (month / now.getDate()) * daysInMonth,
      daily: DAILY, ledger, topup: "manual", funding: "prepaid",
    })
  }),
  http.post("/api/credits/topup", async ({ request }) => {
    await wait(600)
    if (!admin()) return forbidden()
    const { amount_usd } = (await request.json()) as { amount_usd: number }
    balance += amount_usd
    ledger.unshift({ id: `l${Date.now()}`, at: new Date().toISOString(), kind: "topup", amount_usd, by: me()!.name, note: "Manual grant" })
    return HttpResponse.json({ balance_usd: balance })
  }),
  // Codes the mock knows: "WELCOME" adds $25 once; anything else is refused.
  http.post("/api/credits/redeem", async ({ request }) => {
    await wait(500)
    if (!admin()) return forbidden()
    const { code } = (await request.json()) as { code: string }
    if (code.trim().toUpperCase() !== "WELCOME" || redeemed) {
      const detail = redeemed ? "That code was already redeemed here." : "That code is not valid. Check it and try again."
      return HttpResponse.json({ detail }, { status: 422 })
    }
    redeemed = true
    balance += 25
    ledger.unshift({ id: `l${Date.now()}`, at: new Date().toISOString(), kind: "promo", amount_usd: 25, by: me()!.name, note: "Promo code" })
    return HttpResponse.json({ balance_usd: balance, amount_usd: 25, starts_at: null })
  }),
  http.get("/api/credits/spend", async ({ request }) => {
    await wait()
    if (!admin()) return forbidden()
    const u = new URL(request.url)
    const f = PERIOD_SCALE[u.searchParams.get("period") ?? "30d"] ?? 1
    const rows = { person: PERSON, agent: AGENT_ROWS, channel: CHANNEL }[u.searchParams.get("by") ?? "person"] ?? PERSON
    return HttpResponse.json(scale(rows, f))
  }),
  http.get("/api/credits/allowances", async () => {
    await wait()
    return admin() ? HttpResponse.json(allowances) : forbidden()
  }),
  http.post("/api/credits/allowances", async ({ request }) => {
    await wait(300)
    if (!admin()) return forbidden()
    const a = (await request.json()) as Omit<Allowance, "id" | "used_usd">
    // The server counts a group's members; the mock people stand in for them.
    const members = a.subject.kind === "group" ? (a.subject.id === "everyone" ? MEMBERS : 3) : undefined
    const row = { ...a, id: `al${Date.now()}`, used_usd: 0, members }
    allowances.unshift(row)
    return HttpResponse.json(row)
  }),
  http.delete("/api/credits/allowances/:id", ({ params }) => {
    if (!admin()) return forbidden()
    const i = allowances.findIndex((a) => a.id === params.id)
    if (i >= 0) allowances.splice(i, 1)
    return HttpResponse.json({ ok: true })
  }),
]

// Admin: people, invites, agent web access, audit log and workspace settings.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { AdminPerson, AgentAccess, AuditEntry, Invite, WorkspaceSettings } from "@/modules/admin/types"
import { forbidden, notFound, paged, wait } from "./util"
import { agent, AGENTS, DOMAIN, me } from "./world"
import { face } from "./face"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const inDays = (d: number) => new Date(Date.now() + d * 86_400_000).toISOString()
const P = (id: string, name: string, role: AdminPerson["role"], groups: string[], seen: number | null, credit: [number, number] | null, extra: AdminPerson["identities"] = []): AdminPerson => ({
  id, name, role, groups, status: "active", last_seen: seen == null ? null : ago(seen),
  email: role === "visitor" ? "jordan@brightwave.example" : `${name.toLowerCase().replace(/ /g, ".").normalize("NFD").replace(/[̀-ͯ]/g, "")}@${DOMAIN}`,
  identities: role === "visitor" ? [{ provider: "invite", provider_label: "Invite link", icon: "link", label: "jordan@brightwave.example" }] : [{ provider: "discord", provider_label: "Discord", label: name.split(" ")[0]!.toLowerCase() }, ...extra],
  credit: credit && { left_usd: credit[0], total_usd: credit[1] },
})

const people: AdminPerson[] = [
  P("p_maya", "Maya Lindqvist", "owner", ["Leadership", "Platform"], 0.2, [63.2, 100], [{ provider: "oidc", provider_label: "Fernhill", label: `maya.lindqvist@${DOMAIN}` }]),
  P("p_jonas", "Jonas Weber", "admin", ["Platform"], 6, [81.75, 100], [{ provider: "oidc", provider_label: "Fernhill", label: `jonas.weber@${DOMAIN}` }]),
  P("p_viktor", "Viktor Petrov", "admin", ["Leadership", "Sales"], 7, [92.1, 100]),
  P("p_priya", "Priya Raman", "member", ["CLV"], 12, [12.4, 40], [{ provider: "slack", provider_label: "Slack", label: "priya" }]),
  P("p_aiko", "Aiko Tanaka", "member", ["Support", "MMM"], 3, [25.1, 40]),
  P("p_tomas", "Tomás Okafor", "member", ["MMM"], 1, [15.85, 40]),
  P("p_rafael", "Rafael Costa", "member", ["MMM"], 20, [30.6, 40]),
  P("p_owen", "Owen Brooks", "member", ["Pricing"], 0.9, [35.15, 40]),
  P("p_ines", "Inês Duarte", "member", ["CLV", "Pricing"], 1, [40, 40], [{ provider: "microsoft", provider_label: "Microsoft", label: "ines.duarte" }]),
  P("p_lena", "Lena Fischer", "member", ["Marketing"], 9, [38.2, 40]),
  P("p_jordan", "Jordan Ellis", "visitor", [], 1, [3.85, 5]),
]
people[9]!.status = "suspended"

const invites: Invite[] = [
  { id: "inv_demo", email: "jordan@brightwave.example", role: "visitor", agents: [agent("ag_pricing")], credit_usd: 5, expires_at: inDays(14), status: "accepted", by: "Maya Lindqvist" },
  { id: "inv_2", email: "sam.ortiz@larkspur.example", role: "visitor", agents: [agent("ag_mmm")], credit_usd: 10, expires_at: inDays(6), status: "pending", by: "Jonas Weber" },
  { id: "inv_3", email: "marco.bianchi@fernhill.example", role: "member", agents: [], credit_usd: 40, expires_at: inDays(2), status: "pending", by: "Maya Lindqvist" },
  { id: "inv_4", email: "dana.ko@tidewater.example", role: "visitor", agents: [agent("ag_pricing"), agent("ag_sales")], credit_usd: 5, expires_at: inDays(-3), status: "expired", by: "Viktor Petrov" },
]

const access: AgentAccess[] = [
  ...AGENTS.map((a): AgentAccess => ({
    agent: a, web: a.id !== "ag_review", fixed_to: null,
    who: a.id === "ag_pricing" ? "groups" : a.id === "ag_sales" ? "groups" : "everyone",
    groups: a.id === "ag_pricing" ? ["Pricing", "CLV"] : a.id === "ag_sales" ? ["Sales", "Leadership"] : [],
  })),
  { agent: { id: "ag_northwind", name: "Northwind Support", avatar: face(39), model: "Opus 5.5", description: "Northwind customer support" },
    web: false, who: "everyone", groups: [], fixed_to: "#northwind-support" },
]

const audit: AuditEntry[] = [
  { id: "a1", at: ago(0.4), who: "Maya Lindqvist", action: "Changed agent", target: "MMM Analyst", detail: "Instructions, version 14" },
  { id: "a2", at: ago(2), who: "Jonas Weber", action: "Created invite", target: "sam.ortiz@larkspur.example", detail: "Visitor, MMM Analyst, $10.00" },
  { id: "a3", at: ago(5), who: "Maya Lindqvist", action: "Added allowance", target: "MMM group", detail: "$60.00 a month, MMM Analyst" },
  { id: "a4", at: ago(9), who: "Jordan Ellis", action: "Accepted invite", target: "Pricing Analyst" },
  { id: "a5", at: ago(26), who: "Viktor Petrov", action: "Suspended", target: "Lena Fischer" },
  { id: "a6", at: ago(30), who: "Maya Lindqvist", action: "Topped up", target: "Balance", detail: "$500.00" },
  { id: "a7", at: ago(51), who: "Jonas Weber", action: "Turned off web chat", target: "Code Reviewer" },
  { id: "a8", at: ago(74), who: "Maya Lindqvist", action: "Changed role", target: "Jonas Weber", detail: "Member to Admin" },
  { id: "a9", at: ago(98), who: "Jonas Weber", action: "Changed setting", target: "Sign-in", detail: "Email link on" },
]

const settings: WorkspaceSettings = {
  modules: { chat: true, agents: true, credits: true },
  signin: [
    { id: "discord", label: "Discord", enabled: true },
    { id: "slack", label: "Slack", enabled: true },
    { id: "microsoft", label: "Microsoft", enabled: false },
    { id: "oidc", label: "Fernhill", enabled: true },
  ],
  default_agent: "ag_daimon",
  apps: [{ key: "almanac", name: "Almanac", icon: "database", url: "http://localhost:5173" }],
}

const admin = () => me()?.capabilities.includes("people.manage")
const log = (action: string, target: string, detail?: string) =>
  audit.unshift({ id: `a${Date.now()}`, at: new Date().toISOString(), who: me()!.name, action, target, detail })

export const adminHandlers: HttpHandler[] = [
  http.get("/api/admin/*", async () => { await wait(); if (!admin()) return forbidden() }),
  // The list leaves credit out; the person has it.
  http.get("/api/admin/people", ({ request }) => {
    const q = (new URL(request.url).searchParams.get("q") ?? "").trim().toLowerCase()
    const found = people.filter((p) => !q || p.name.toLowerCase().includes(q) || p.email.toLowerCase().includes(q))
    return paged(request, found.map((p) => ({ ...p, credit: null })), 200)
  }),
  // Everyone first, then each group the people carry. Visitors are in no group.
  http.get("/api/admin/groups", () => {
    const counts = new Map<string, number>()
    for (const p of people) for (const g of p.groups) counts.set(g, (counts.get(g) ?? 0) + 1)
    const members = people.filter((p) => p.role !== "visitor").length
    return HttpResponse.json([{ id: "everyone", name: "Everyone", members }, ...[...counts].sort(([a], [b]) => a.localeCompare(b)).map(([name, n]) => ({ id: name, name, members: n }))])
  }),
  http.get("/api/admin/people/:id", ({ params }) => {
    const p = people.find((x) => x.id === params.id)
    return p ? HttpResponse.json(p) : notFound()
  }),
  http.patch("/api/admin/people/:id", async ({ params, request }) => {
    const p = people.find((x) => x.id === params.id)
    if (!p) return notFound()
    const body = (await request.json()) as Partial<AdminPerson>
    if (body.role && body.role !== p.role) log("Changed role", p.name, `${p.role} to ${body.role}`)
    if (body.status && body.status !== p.status) log(body.status === "suspended" ? "Suspended" : "Restored", p.name)
    Object.assign(p, body)
    return HttpResponse.json(p)
  }),
  http.get("/api/admin/invites", ({ request }) => paged(request, invites)),
  http.post("/api/admin/invites", async ({ request }) => {
    await wait(400)
    const b = (await request.json()) as { email: string; role: Invite["role"]; agents: string[]; credit_usd: number; days: number }
    const inv: Invite = {
      id: `inv_${Date.now().toString(36)}`, email: b.email, role: b.role, agents: b.agents.map(agent), credit_usd: b.credit_usd,
      expires_at: inDays(b.days), status: "pending", by: me()!.name,
    }
    inv.link = `${location.origin}/invite/inv_demo`
    invites.unshift(inv)
    log("Created invite", b.email)
    return HttpResponse.json(inv)
  }),
  http.delete("/api/admin/invites/:id", ({ params }) => {
    const i = invites.findIndex((x) => x.id === params.id)
    if (i >= 0) { log("Revoked invite", invites[i]!.email); invites.splice(i, 1) }
    return HttpResponse.json({ ok: true })
  }),
  http.get("/api/admin/agent-access", () => HttpResponse.json(access)),
  http.patch("/api/admin/agent-access/:id", async ({ params, request }) => {
    const a = access.find((x) => x.agent.id === params.id)
    if (!a || a.fixed_to) return forbidden()
    const b = (await request.json()) as Partial<AgentAccess>
    if (b.web !== undefined && b.web !== a.web) log(b.web ? "Turned on web chat" : "Turned off web chat", a.agent.name)
    Object.assign(a, b)
    return HttpResponse.json(a)
  }),
  http.post("/api/admin/people/:id/link", async ({ params }) => {
    await wait(300)
    const p = people.find((x) => x.id === params.id)
    if (!p || p.role !== "visitor") return notFound()
    log("Created sign-in link", p.name)
    return HttpResponse.json({ link: `${location.origin}/invite/inv_${Math.random().toString(36).slice(2, 10)}` })
  }),
  http.get("/api/admin/audit", ({ request }) => paged(request, audit)),
  http.get("/api/admin/apps/status", () => HttpResponse.json(Object.fromEntries(settings.apps.map((a) => [a.key, "connected"])))),
  http.get("/api/admin/settings", () => HttpResponse.json(settings)),
  http.patch("/api/admin/settings", async ({ request }) => {
    const { signin, ...b } = (await request.json()) as Partial<Omit<WorkspaceSettings, "signin">> & { signin?: Record<string, boolean> }
    Object.assign(settings, b)
    for (const m of settings.signin) if (signin && m.id in signin) m.enabled = signin[m.id]!
    log("Changed setting", "Workspace")
    return HttpResponse.json(settings)
  }),
]

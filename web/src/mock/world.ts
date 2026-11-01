// The fictional org every mock handler draws from. Invented names.
import type { AgentRef, Capability, ChatSession, Me, ModuleKey, Person, Role, Workspace } from "@/lib/types"
import { face } from "./face"

export const DOMAIN = "fernhill.example"

export const PEOPLE: (Person & { identities: Me["identities"] })[] = [
  { id: "p_maya", name: "Maya Lindqvist", email: `maya.lindqvist@${DOMAIN}`, role: "owner", title: "Head of AI Platform",
    identities: [{ provider: "discord", provider_label: "Discord", label: "maya.l" }] },
  { id: "p_jonas", name: "Jonas Weber", email: `jonas.weber@${DOMAIN}`, role: "admin", title: "ML Engineer",
    identities: [{ provider: "discord", provider_label: "Discord", label: "jonasw" }] },
  { id: "p_priya", name: "Priya Raman", email: `priya.raman@${DOMAIN}`, role: "member", title: "Senior Data Scientist",
    identities: [{ provider: "discord", provider_label: "Discord", label: "priya.r" }] },
  { id: "p_jordan", name: "Jordan Ellis", email: "jordan@brightwave.example", role: "visitor", title: "Brightwave Telecom",
    identities: [{ provider: "invite", provider_label: "Invite link", icon: "link", label: "jordan@brightwave.example" }] },
]

export const WORKSPACES: Workspace[] = [
  { id: "ws_fernhill", name: "Fernhill Labs", platform: "discord", role: "owner", members: 16 },
  { id: "ws_northwind", name: "Northwind Retail", platform: "slack", role: "member", members: 42 },
]


/** Agents reachable on the web. Northwind Support is fixed to a confidential channel, so it is absent here. */
export const AGENTS: AgentRef[] = [
  { id: "ag_daimon", name: "Daimon", avatar: face(11), model: "Sonnet 5.5", description: "General assistant",
    starters: ["What changed in Almanac this week?", "Email the readout to the team", "Draft a client update"] },
  { id: "ag_mmm", name: "MMM Analyst", avatar: face(4), model: "Opus 5.5", description: "Media mix models with PyMC-Marketing",
    starters: ["Summarize the Q3 readout for Larkspur", "Which channels have the widest intervals?"] },
  { id: "ag_pricing", name: "Pricing Analyst", avatar: face(24), model: "Sonnet 5.5", description: "Price elasticity and promo lift",
    starters: ["Summarize the latest price test", "Does the result hold on mobile?"] },
  { id: "ag_review", name: "Code Reviewer", avatar: face(33), model: "Sonnet 5.5", description: "Reviews pull requests",
    starters: ["Review PR #418", "What changed in adstock.py this week?"] },
  { id: "ag_sales", name: "Sales Desk", avatar: face(14), model: "Haiku 5.5", description: "Account briefs from the CRM",
    starters: ["Brief me on Brightwave", "Connect HubSpot", "Open deals over $50k"] },
]
export const agent = (id: string) => AGENTS.find((a) => a.id === id)!

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()

export const SESSIONS: Record<string, ChatSession[]> = {
  p_maya: [
    { id: "s_q3", title: "Q3 media mix readout for Larkspur", agent: agent("ag_mmm"), updated_at: hoursAgo(0.3) },
    { id: "s_churn", title: "Cobalt churn model priors", agent: agent("ag_daimon"), updated_at: hoursAgo(3) },
    { id: "s_pr", title: "Review PR #412 adstock refactor", agent: agent("ag_review"), updated_at: hoursAgo(26) },
    { id: "s_tide", title: "Tidewater promo elasticity", agent: agent("ag_pricing"), updated_at: hoursAgo(50) },
    { id: "s_brief", title: "Brightwave account brief", agent: agent("ag_sales"), updated_at: hoursAgo(80) },
    { id: "s_onboard", title: "Onboarding checklist for Marco", agent: agent("ag_daimon"), updated_at: hoursAgo(200) },
    { id: "s_hiring", title: "Draft data scientist job post", agent: agent("ag_daimon"), updated_at: hoursAgo(260) },
  ],
  p_jordan: [
    { id: "s_vis1", title: "Price test for the unlimited plan", agent: agent("ag_pricing"), updated_at: hoursAgo(1) },
  ],
}

const ALL: Capability[] = [
  "chat.use", "agents.view", "agents.edit", "agents.create", "credits.view_own", "credits.manage", "people.manage", "audit.view", "workspace.manage",
]
const CAPS: Record<Role, Capability[]> = {
  owner: ALL, admin: ALL,
  member: ["chat.use", "agents.view", "agents.edit", "agents.create", "credits.view_own"],
  visitor: ["chat.use", "credits.view_own"],
}
const MODULES: Record<Role, ModuleKey[]> = {
  owner: ["chat", "agents", "credits", "admin"], admin: ["chat", "agents", "credits", "admin"],
  member: ["chat", "agents", "credits"], visitor: ["chat"],
}
const CREDIT: Record<Role, Me["credit"]> = {
  owner: { left_usd: 63.2, total_usd: 100, window: "month", resets_at: "2026-11-01T00:00:00Z" },
  admin: { left_usd: 81.75, total_usd: 100, window: "month", resets_at: "2026-11-01T00:00:00Z" },
  member: { left_usd: 12.4, total_usd: 40, window: "month", resets_at: "2026-11-01T00:00:00Z" },
  visitor: { left_usd: 3.85, total_usd: 5, window: "total", resets_at: null },
}

// Mock session state lives in localStorage so screenshots can sign in as anyone.
const K_USER = "daimon:mock:user"
const K_WS = "daimon:mock:ws"

export const session = {
  user: () => PEOPLE.find((p) => p.email === localStorage.getItem(K_USER)) ?? null,
  signIn: (email: string) => localStorage.setItem(K_USER, email),
  signOut: () => localStorage.removeItem(K_USER),
  workspace: () => localStorage.getItem(K_WS) ?? "ws_fernhill",
  setWorkspace: (id: string) => localStorage.setItem(K_WS, id),
}

// Credit spent by turns in this tab. `?credit=out` (or `?credit=0.5`) at load sets everyone's left amount; `?credit=none` leaves nobody governed.
const K_CREDIT = "daimon:mock:credit"
try {
  const flag = new URLSearchParams(location.search).get("credit")
  if (flag !== null) localStorage.setItem(K_CREDIT, flag === "out" ? "0" : flag)
} catch { /* no storage */ }
const spent: Record<string, number> = {}
export const spend = (pid: string, usd: number) => { spent[pid] = (spent[pid] ?? 0) + usd }
function live(pid: string, c: Me["credit"]): Me["credit"] {
  if (!c || localStorage.getItem(K_CREDIT) === "none") return null
  const o = Number(localStorage.getItem(K_CREDIT) ?? NaN)
  const base = Number.isNaN(o) ? c.left_usd : o
  return { ...c, left_usd: Math.max(0, +(base - (spent[pid] ?? 0)).toFixed(2)) }
}

export function me(): Me | null {
  const p = session.user()
  if (!p) return null
  const wss = p.role === "visitor" ? [{ ...WORKSPACES[0]!, role: "visitor" as Role }] : WORKSPACES.map((w) => (
    w.id === "ws_fernhill" ? { ...w, role: p.role } : w
  ))
  const ws = wss.find((w) => w.id === session.workspace()) ?? wss[0]!
  const role = ws.role
  return {
    id: p.id, name: p.name, email: p.email, workspace: ws, workspaces: wss, role,
    modules: MODULES[role], capabilities: CAPS[role], identities: p.identities, credit: live(p.id, CREDIT[role]),
    invited_by: role === "visitor" ? "Maya Lindqvist" : undefined,
  }
}

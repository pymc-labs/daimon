// Integrations: chat installs, GitHub, tool servers and API tokens for Fernhill Labs.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { ApiToken, Github, Mcp, McpExpiry, Minted, Tokens, Workspaces } from "@/modules/integrations/types"
import { forbidden, wait } from "./util"
import { me } from "./world"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const ahead = (d: number) => new Date(Date.now() + d * 86_400_000).toISOString()

const workspaces: Workspaces = {
  workspaces: [
    { platform: "discord", name: "Fernhill Labs", status: "connected", problem: false, teams: [] },
    { platform: "slack", name: "Fernhill Labs", status: "disconnected", problem: true, teams: [] },
    { platform: "teams", name: "Fernhill Labs", status: "connected", problem: false, teams: [{ id: "t1", name: "Data science" }, { id: "t2", name: null }] },
  ],
}

const github: Github = {
  configured: true,
  installations: [
    { id: "9", account: "fernhill-labs", selection: "selected", repos: 7, state: "active", refreshed_at: ago(3) },
    { id: "12", account: "fernhill-oss", selection: "all", repos: 2, state: "suspended", refreshed_at: ago(90) },
  ],
  agents: [
    { id: "ag_review", name: "Code Reviewer", repos: [
      { name: "fernhill-labs/mmm-core", access: "read", shared: true, status: "active", working: false },
      { name: "fernhill-labs/adstock", access: "write", shared: false, status: "active", working: true },
      { name: "fernhill-oss/docs", access: "read", shared: true, status: "suspended", working: false },
    ] },
    { id: "ag_mmm", name: "MMM Analyst", repos: [{ name: "fernhill-labs/mmm-core", access: "write", shared: true, status: "active", working: true }] },
  ],
  requests: [
    { id: "r1", agent: "Pricing Analyst", repos: ["fernhill-labs/pricing-models"], access: "write", requester: "Priya Raman", created_at: ago(5), expires_at: ahead(2), waiting_github: false },
    { id: "r2", agent: "Sales Desk", repos: ["fernhill-labs/crm-sync", "fernhill-labs/decks"], access: "read", requester: "Aiko Tanaka", created_at: ago(30), expires_at: ahead(1), waiting_github: true },
  ],
  trouble: [
    { kind: "push", subject: "fernhill-labs/mmm-core refs/heads/main", attempts: 3, error: "2 agents failed to sync", at: ago(2) },
    { kind: "installation", subject: "fernhill-oss", attempts: 5, error: "GitHub says the install is paused", at: ago(26) },
  ],
}

const mcp: Mcp = {
  servers: [
    { id: "a1b2c3d4e5f6", name: "Linear", url: "https://mcp.linear.app/sse", signed_in: 5, agents: [
      { id: "ag_daimon", name: "Daimon", shared: false, signed_in: 4 }, { id: "ag_sales", name: "Sales Desk", shared: false, signed_in: 1 },
    ] },
    { id: "b2c3d4e5f6a1", name: "Notion", url: "https://mcp.notion.com/mcp", signed_in: 3, agents: [{ id: "ag_daimon", name: "Daimon", shared: false, signed_in: 3 }] },
    { id: "c3d4e5f6a1b2", name: "Warehouse", url: "https://warehouse.fernhill.example/mcp", signed_in: 0, agents: [
      { id: "ag_mmm", name: "MMM Analyst", shared: true, signed_in: 0 }, { id: "ag_pricing", name: "Pricing Analyst", shared: true, signed_in: 0 },
    ] },
  ],
}
const expiry: McpExpiry = { complete: true, servers: [{ id: "a1b2c3d4e5f6", expired: 2, people: ["Tomás Okafor", "Owen Brooks"] }] }

const SCOPES = [
  { id: "tenant:read", label: "Read the workspace summary" }, { id: "channels:write", label: "Change channel settings" },
  { id: "agents:archive", label: "Archive channel copies" }, { id: "promo:redeem", label: "Redeem promo codes" },
]
const tokens: ApiToken[] = [
  { id: "tk1", label: "Status dashboard", scopes: ["tenant:read"], created_at: ago(24 * 6), expires_at: ahead(24) },
  { id: "tk2", label: null, scopes: ["tenant:read", "channels:write"], created_at: ago(24 * 20), expires_at: ahead(10) },
]

const admin = () => me()?.capabilities.includes("integrations.view")
const writer = () => me()?.capabilities.includes("integrations.manage")
let n = 0

export const integrationsHandlers: HttpHandler[] = [
  http.get("/api/integrations/workspaces", async () => { await wait(); return admin() ? HttpResponse.json(workspaces) : forbidden() }),
  http.get("/api/integrations/github", async () => { await wait(); return admin() ? HttpResponse.json(github) : forbidden() }),
  http.get("/api/integrations/mcp", async () => { await wait(); return admin() ? HttpResponse.json(mcp) : forbidden() }),
  http.get("/api/integrations/mcp/expiry", async () => { await wait(1400); return admin() ? HttpResponse.json(expiry) : forbidden() }),
  http.get("/api/integrations/tokens", async () => {
    await wait()
    const body: Tokens = { configured: true, scopes: SCOPES, days: 30, tokens }
    return admin() ? HttpResponse.json(body) : forbidden()
  }),
  http.post("/api/integrations/tokens", async ({ request }) => {
    await wait(400)
    if (!writer()) return forbidden()
    const b = (await request.json()) as { scopes: string[]; label?: string }
    if (!b.scopes.length || b.scopes.some((s) => !SCOPES.some((x) => x.id === s))) {
      return HttpResponse.json({ detail: "Pick at least one permission from the list." }, { status: 422 })
    }
    const id = `tk${Date.now()}`
    const row: ApiToken = { id, label: b.label ?? null, scopes: b.scopes, created_at: new Date().toISOString(), expires_at: ahead(30) }
    tokens.unshift(row)
    const out: Minted = { token: `mock-token-${++n}-not-a-real-credential`, id, scopes: row.scopes, expires_at: row.expires_at! }
    return HttpResponse.json(out, { headers: { "Cache-Control": "no-store" } })
  }),
  http.delete("/api/integrations/tokens/:id", async ({ params }) => {
    await wait(200)
    if (!writer()) return forbidden()
    const i = tokens.findIndex((t) => t.id === params.id)
    if (i < 0) return HttpResponse.json({ detail: "This API token isn't here." }, { status: 404 })
    tokens.splice(i, 1)
    return new HttpResponse(null, { status: 204 })
  }),
]

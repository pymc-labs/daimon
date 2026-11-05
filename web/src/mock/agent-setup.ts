// Mock API for what an agent holds beyond its config: keys, coding keys, owners, knowledge, lifecycle.
import { http, HttpResponse } from "msw"

import { CODING_CLIENTS } from "@/modules/agents/setup/clients"
import type { AgentKey, AgentToolRule, CodingKey, ConnectBatch, ConnectStatus, MintedCodingKey } from "@/modules/agents/types"
import { roster } from "./agents"
import { forbidden, notFound, unauthorized, wait } from "./util"
import { PEOPLE } from "./world"

const RULES = new Map<string, AgentToolRule[]>()
const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const nameOf = (id: string) => PEOPLE.find((p) => p.id === id)?.name ?? "Daimon"

/** Who sees an agent's setup: the same rule the agent detail applies. */
function setupOf(id: string) {
  const v = roster.viewer(), r = roster.find(id)
  if (!v) return { res: unauthorized() }
  if (!r) return { res: notFound() }
  if (!roster.details(r, v)) return { res: forbidden("Keys are for the agent's owner or an admin.") }
  return { v, r }
}

const KEYS = new Map<string, AgentKey[]>([
  ["ag_mmm", [{ key: "WAREHOUSE_API_KEY", updated_at: ago(70) }, { key: "SLACK_BOT_TOKEN", updated_at: ago(300) }]],
  ["ag_sales", [{ key: "HUBSPOT_ACCESS_TOKEN", updated_at: ago(120) }]],
])
const CODING = new Map<string, (CodingKey & { by: string })[]>([
  ["ag_mmm", [{ id: "ck_1", creator: "Maya Lindqvist", by: "p_maya", created_at: ago(240), expires_at: new Date(Date.now() + 60 * 86_400_000).toISOString(), mine: false, client: "claude-code" }]],
])
/** Connect batches by id: when each was minted, or null once replaced. A live batch reads as connected about 20 seconds on. */
const BATCHES = new Map<string, number | null>()
const PROMPT_TTL_MS = 10 * 60_000
const CLAIM_URL = "https://daimon.example.com/coding-keys/claim"
const MCP_URL = "https://daimon.example.com/mcp"

/** Where each tool keeps its MCP servers, the entry it holds there, and what to do after. */
const TOOLS: Record<string, { where: string; after: string; entry: (name: string) => string }> = {
  "claude-code": { where: 'the top-level "mcpServers" object in ~/.claude.json', after: "restart Claude Code", entry: (n) => `"${n}": {"type": "http", "url": "${MCP_URL}", "headers": {"Authorization": "Bearer <key>"}}` },
  codex: { where: "~/.codex/config.toml, replacing any existing table for this server", after: "restart Codex", entry: (n) => `[mcp_servers.${n}]\nurl = "${MCP_URL}"\nhttp_headers = { Authorization = "Bearer <key>" }` },
  cursor: { where: 'the "mcpServers" object in ~/.cursor/mcp.json', after: "reload Cursor", entry: (n) => `"${n}": {"url": "${MCP_URL}", "headers": {"Authorization": "Bearer <key>"}}` },
  pi: { where: 'the "mcpServers" object in ~/.pi/agent/mcp.json', after: "run /reload", entry: (n) => `"${n}": {"url": "${MCP_URL}", "headers": {"Authorization": "Bearer <key>"}, "exposure": "direct"}` },
  mcp: { where: "your MCP config, as a streamable HTTP server", after: "reload your MCP servers", entry: (n) => `name: ${n}\nurl: ${MCP_URL}\nheader: Authorization: Bearer <key>` },
}
/** The real prompt's shape: no agent name but the server key's slug. */
const slugOf = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 32).replace(/^-+|-+$/g, "") || "agent"
const PROMPT_TEXT = (tool: string, slug: string, code: string) => {
  const t = TOOLS[tool]
  return (
    "Connect yourself to a Daimon agent as a remote MCP server. " +
    `Add this to ${t.where}, keeping everything else:\n\n${t.entry(`daimon-${slug}`)}\n\n` +
    "Get <key> with ONE shell command that runs " +
    `\`curl -fsS -X POST ${CLAIM_URL} -d code=${code}\` (the response body is the bare key) ` +
    "and writes it straight into the file, so it never appears in this chat. " +
    "Never print, read back or echo it. If the command exits non-zero or prints nothing, " +
    "stop and tell me what it said; do not retry or make up a key.\n\n" +
    `Then tell me to ${t.after}. The code works once, within 10 minutes.`
  )
}

const isAdmin = () => roster.viewer()?.admin === true

export const agentSetupHandlers = [
  http.get("/api/agents/:id/owners/options", async () => {
    await wait()
    if (!roster.viewer()) return unauthorized()
    if (!isAdmin()) return forbidden("Changing owners needs an admin.")
    return HttpResponse.json(PEOPLE.filter((p) => p.role !== "visitor").map((p) => ({ id: p.id, name: p.name })))
  }),
  http.put("/api/agents/:id/owners", async ({ params, request }) => {
    await wait(300)
    if (!roster.viewer()) return unauthorized()
    if (!isAdmin()) return forbidden("Changing owners needs an admin.")
    const r = roster.find(String(params.id))
    if (!r) return notFound()
    const { owner, co_owners } = (await request.json()) as { owner: string | null; co_owners: string[] }
    r.owner = owner ?? ""
    r.co_owners = [...new Set(co_owners)].filter((id) => id !== owner)
    roster.save()
    return new HttpResponse(null, { status: 204 })
  }),
  http.get("/api/agents/:id/keys", async ({ params }) => {
    await wait()
    const s = setupOf(String(params.id))
    return s.res ?? HttpResponse.json(KEYS.get(String(params.id)) ?? [])
  }),
  http.put("/api/agents/:id/keys/:key", async ({ params, request }) => {
    await wait(300)
    const id = String(params.id), key = String(params.key)
    const s = setupOf(id)
    if (s.res) return s.res
    const { value } = (await request.json()) as { value: string }
    if (!value.trim()) return HttpResponse.json({ detail: "Enter the value first." }, { status: 422 })
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return HttpResponse.json({ detail: `${key} is not a valid key name (letters, digits, underscores; not leading digit).` }, { status: 422 })
    const row = { key, updated_at: new Date().toISOString() }
    KEYS.set(id, [...(KEYS.get(id) ?? []).filter((k) => k.key !== key), row].sort((a, b) => a.key.localeCompare(b.key)))
    return HttpResponse.json(row)
  }),
  http.delete("/api/agents/:id/keys/:key", async ({ params }) => {
    await wait(300)
    const id = String(params.id), key = String(params.key)
    const s = setupOf(id)
    if (s.res) return s.res
    const held = (KEYS.get(id) ?? []).some((k) => k.key === key)
    KEYS.set(id, (KEYS.get(id) ?? []).filter((k) => k.key !== key))
    return HttpResponse.json({ removed: held })
  }),
  http.get("/api/agents/:id/knowledge", async ({ params }) => {
    await wait()
    const s = setupOf(String(params.id))
    if (s.res) return s.res
    return HttpResponse.json({ shared: true, channels: s.v.admin ? [{ store: "discord/c_clv", platform: "discord", name: "client-ventures" }] : [] })
  }),
  http.get("/api/agents/:id/knowledge/paths", async ({ params, request }) => {
    await wait()
    const s = setupOf(String(params.id))
    if (s.res) return s.res
    const store = new URL(request.url).searchParams.get("store")
    if (store === "shared") return HttpResponse.json(["/style.md", "/glossary.md"])
    if (store && s.v.admin) return HttpResponse.json(["/room.md"])
    return notFound()
  }),
  http.get("/api/agents/:id/knowledge/content", async ({ params, request }) => {
    await wait()
    const s = setupOf(String(params.id))
    if (s.res) return s.res
    const path = new URL(request.url).searchParams.get("path") ?? ""
    return HttpResponse.json({ path, content: `Notes kept in ${path}.\nUse short sentences.` })
  }),
  http.get("/api/agents/:id/tool-rules", async ({ params }) => {
    await wait()
    if (!isAdmin()) return forbidden("Tool rules are for admins.")
    if (!roster.find(String(params.id))) return notFound()
    return HttpResponse.json({ active: true, rules: RULES.get(String(params.id)) ?? [] })
  }),
  http.put("/api/agents/:id/tool-rules", async ({ params, request }) => {
    await wait()
    const id = String(params.id)
    if (!isAdmin()) return forbidden("Tool rules are for admins.")
    if (!roster.find(id)) return notFound()
    const { expected_revision, ...b } = (await request.json()) as Omit<AgentToolRule, "revision"> & { expected_revision: number }
    const rows = RULES.get(id) ?? []
    const prior = rows.find((r) => r.tool === b.tool)
    if ((prior?.revision ?? 0) !== expected_revision) return HttpResponse.json({ detail: "This rule changed since you opened it. Reload and try again." }, { status: 409 })
    const next = { ...b, revision: expected_revision + 1 }
    RULES.set(id, [...rows.filter((r) => r !== prior), next])
    return HttpResponse.json(next)
  }),
  http.get("/api/agents/:id/google", async ({ params }) => {
    await wait()
    const s = setupOf(String(params.id))
    if (s.res) return s.res
    return HttpResponse.json({ email: "assistant@example.com", scopes: ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/calendar.readonly"] })
  }),
  http.get("/api/agents/:id/coding-keys", async ({ params }) => {
    await wait()
    const s = setupOf(String(params.id))
    if (s.res) return s.res
    const mine = s.v.id
    const rows = (CODING.get(String(params.id)) ?? []).filter((k) => s.v.admin || k.by === mine)
    return HttpResponse.json(rows.map(({ by, ...k }) => ({ ...k, mine: by === mine })))
  }),
  http.post("/api/agents/:id/coding-keys", async ({ params }) => {
    await wait(400)
    const id = String(params.id)
    const s = setupOf(id)
    if (s.res) return s.res
    const token = `mock.${Math.random().toString(36).slice(2)}.${Math.random().toString(36).slice(2)}`
    const expires = new Date(Date.now() + (s.v.admin ? 90 : 30) * 86_400_000).toISOString()
    const key = `ck_${Date.now().toString(36)}`
    CODING.set(id, [{ id: key, creator: nameOf(s.v.id), by: s.v.id, created_at: new Date().toISOString(), expires_at: expires, mine: true, client: "token" }, ...(CODING.get(id) ?? [])])
    const out: MintedCodingKey = { id: key, token, url: "https://daimon.example.com/mcp", expires_at: expires }
    return HttpResponse.json(out)
  }),
  http.post("/api/agents/:id/coding-keys/connect", async ({ params, request }) => {
    await wait(300)
    const s = setupOf(String(params.id))
    if (s.res) return s.res
    const body = (await request.json().catch(() => ({}))) as { replaces?: string }
    if (body.replaces && BATCHES.has(body.replaces)) BATCHES.set(body.replaces, null)
    const batch = `cb_${Date.now().toString(36)}`
    BATCHES.set(batch, Date.now())
    const slug = slugOf(s.r.versions.at(-1)?.config.name ?? "")
    const out: ConnectBatch = {
      batch, expires_at: new Date(Date.now() + PROMPT_TTL_MS).toISOString(),
      prompts: CODING_CLIENTS.filter((c) => c.id !== "token").map((c) => ({ id: c.id, name: c.name, prompt: PROMPT_TEXT(c.id, slug, `${batch}_${c.id}_${Math.random().toString(36).slice(2, 10)}`) })),
    }
    return HttpResponse.json(out)
  }),
  http.get("/api/agents/:id/coding-keys/connect/:batch", async ({ params }) => {
    await wait(150)
    const s = setupOf(String(params.id))
    if (s.res) return s.res
    const at = BATCHES.get(String(params.batch))
    if (at === undefined) return notFound()
    const out: ConnectStatus = at !== null && Date.now() - at > 20_000 ? { connected: true, client: "codex" } : { connected: false, client: null }
    return HttpResponse.json(out)
  }),
  http.delete("/api/agents/:id/coding-keys/:key", async ({ params }) => {
    await wait(250)
    const id = String(params.id)
    const s = setupOf(id)
    if (s.res) return s.res
    const rows = CODING.get(id) ?? []
    const row = rows.find((k) => k.id === params.key && (s.v.admin || k.by === s.v.id))
    if (!row) return notFound()
    CODING.set(id, rows.filter((k) => k !== row))
    return new HttpResponse(null, { status: 204 })
  }),
  http.post("/api/agents/:id/archive", async ({ params }) => {
    await wait(400)
    if (!roster.viewer()) return unauthorized()
    if (!isAdmin()) return forbidden("Archiving an agent needs an admin.")
    if (!roster.find(String(params.id))) return notFound()
    roster.remove(String(params.id))
    return new HttpResponse(null, { status: 204 })
  }),
  http.post("/api/agents/:id/copy", async ({ params, request }) => {
    await wait(500)
    const v = roster.viewer()
    if (!v) return unauthorized()
    if (!isAdmin()) return forbidden("Copying an agent needs an admin.")
    const r = roster.find(String(params.id))
    if (!r) return notFound()
    const name = ((await request.json()) as { name: string }).name.trim()
    if (!name) return HttpResponse.json({ detail: "That name can't be used." }, { status: 422 })
    if (roster.all().some((x) => x.versions.at(-1)!.config.name.toLowerCase() === name.toLowerCase())) return HttpResponse.json({ detail: `agent '${name}' already exists` }, { status: 409 })
    const made = roster.copy(r, name, v.id)
    return HttpResponse.json({ id: made.id, name })
  }),
]

// Data sources: the Data module, Integrations > Data sources, Admin > Data access, Profile data accounts.
import { http, HttpResponse, type HttpHandler } from "msw"

import type {
  Candidate, Connected, DataRecord, DataSource, Hit, Kind, RedactionDetail, RedactionSet, Run, Scope, Search, SourceIdentity, SourceState,
} from "@/modules/data/types"
import { notFound, paged, wait } from "./util"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()

const sources: SourceState[] = [
  { id: "s_wiki", name: "Team wiki", kind: "notion", mode: "stored", status: "active", publication: "published", bound: false, default_scope: "internal", created_at: ago(900), records: 1284,
    last_sync_at: ago(2), last_status: "ok", last_error: null, errors: 0, parity: { finished_at: ago(300), people: 12, checked: 480, wrong_more: 0, wrong_less: 2, unresolved: null, ok: true }, can_publish: true, blocked: null },
  { id: "s_drive", name: "Shared drive", kind: "gdrive", mode: "stored", status: "active", publication: "shadow", bound: false, default_scope: "internal", created_at: ago(120), records: 342,
    last_sync_at: ago(5), last_status: "ok", last_error: null, errors: 1, parity: { finished_at: ago(20), people: 12, checked: 300, wrong_more: 3, wrong_less: 0, unresolved: null, ok: false }, can_publish: false, blocked: "3 people would see more than before" },
  { id: "s_mail", name: "Support inbox", kind: "gmail", mode: "federated", status: "paused", publication: "published", bound: true, default_scope: "restricted", created_at: ago(2000), records: 0,
    last_sync_at: null, last_status: null, last_error: null, errors: 0, parity: null, can_publish: false, blocked: null },
  { id: "s_crm", name: "Customer notes", kind: "hubspot", mode: "stored", status: "error", publication: "shadow", bound: false, default_scope: "client", created_at: ago(60), records: 58,
    last_sync_at: ago(30), last_status: "failed", last_error: "The credential was refused", errors: 4, parity: null, can_publish: false, blocked: "No parity check yet" },
]
const kinds: Kind[] = [
  { kind: "notion", name: "Notion", category: "Documents", live: false, fields: [
    { name: "token", label: "Token", kind: "secret", options: [], required: true, placeholder: "" },
    { name: "root", label: "Root page", kind: "text", options: [], required: false, placeholder: "Page id" } ] },
  { kind: "gdrive", name: "Google Drive", category: "Documents", live: false, fields: [
    { name: "folders", label: "Folders", kind: "list", options: [], required: true, placeholder: "Folder id" },
    { name: "mode", label: "Mode", kind: "select", options: ["read", "read and index"], required: true, placeholder: "" } ] },
  { kind: "hubspot", name: "HubSpot", category: "CRM", live: true, fields: [{ name: "token", label: "Token", kind: "secret", options: [], required: true, placeholder: "" }] },
]
const runs = (id: string): Run[] => [
  { id: `${id}-3`, trigger: "schedule", status: "ok", started_at: ago(2), finished_at: ago(2), added: 12, updated: 4, deleted: 0, error: null },
  { id: `${id}-2`, trigger: "manual", status: "failed", started_at: ago(26), finished_at: ago(26), added: 0, updated: 0, deleted: 0, error: "The credential was refused" },
  { id: `${id}-1`, trigger: "schedule", status: "ok", started_at: ago(50), finished_at: ago(50), added: 90, updated: 11, deleted: 3, error: null },
]

const items = Array.from({ length: 40 }, (_, i) => ({
  id: `rec${i + 1}`, source_id: i % 3 ? "s_wiki" : "s_drive", kind: i % 3 ? "page" : "file", title: i % 3 ? `Onboarding notes ${i + 1}` : `Quarterly plan ${i + 1}.pdf`,
  url: i % 4 ? "https://example.com/doc" : null, author: "Maya Lindqvist", occurred_at: ago(i * 7 + 1), scope: i % 3 ? "internal" : "client",
}))
const record = (id: string): DataRecord | null => {
  const item = items.find((i) => i.id === id)
  return item ? {
    item, text: "Welcome. This page covers how a new team member gets access, who to ask for help, and where the weekly notes live.\n\nSet up your accounts first, then read the handbook.",
    next_start: null, files: item.kind === "file" ? [{ ord: 0, name: "plan.pdf", content_type: "application/pdf", size: 183_000 }] : [],
  } : null
}

const scopes: Scope[] = [
  { id: "public", kind: "public", name: "Public", description: "Open to everyone", bound: true, holders: [] },
  { id: "internal", kind: "internal", name: "Internal", description: "The whole team", bound: false, holders: [{ holder: "account:a_maya", name: "Maya Lindqvist", bound: false }, { holder: "account:a_priya", name: "Priya Raman", bound: false }] },
  { id: "client", kind: "client", name: "Client work", description: "Client files", bound: false, holders: [{ holder: "account:a_maya", name: "Maya Lindqvist", bound: true }] },
]
const people = [{ account_id: "a_maya", name: "Maya Lindqvist" }, { account_id: "a_priya", name: "Priya Raman" }, { account_id: "a_jordan", name: "Jordan Ellis" }]
const identities: SourceIdentity[] = [
  { provider: "slack", workspace: "T1", subject: "U1", name: "maya", email: "maya@example.com", email_verified: true, account_id: "a_maya", account_name: "Maya Lindqvist", proof: "admin", linked_at: ago(400), redacted: false },
  { provider: "google", workspace: "", subject: "g2", name: "priya.r", email: "priya@example.com", email_verified: true, account_id: "a_priya", account_name: "Priya Raman", proof: "oauth", linked_at: ago(200), redacted: true },
  { provider: "notion", workspace: "W1", subject: "n3", name: "J. Ellis", email: null, email_verified: false, account_id: null, account_name: null, proof: null, linked_at: null, redacted: false },
]
const candidates: Candidate[] = [{ provider: "notion", workspace: "W1", subject: "n3", name: "J. Ellis", email: "jordan@example.com", account_id: "a_jordan", account_name: "Jordan Ellis" }]
const sets: RedactionSet[] = [
  { name: "clients", repo: "example/redaction", path: "clients.yaml", ref: "main", approved_sha: "3f9a1c0d2b", pin_sha: "3f9a1c0d2b", pending: false, usable: true, checked_at: ago(3), error: null },
  { name: "people", repo: "example/redaction", path: "people.yaml", ref: "main", approved_sha: "11aa22bb33", pin_sha: "9c8d7e6f5a", pending: true, usable: true, checked_at: ago(3), error: null },
]
const rules = { aliases: { Larkspur: "Client A" }, removals: ["internal-only"], regex: [{ pattern: "\\d{4}-\\d{4}", replace: "[number]" }], exclude_ids: [], entities: [{ name: "Larkspur", aliases: ["LKS"], scope: "client", label: "Client A" }] }
const connected: Connected = {
  notices: [{ kind: "linked", identity: "slack:T1:user:U1", provider: "slack", name: "maya" }],
  accounts: [{ provider: "slack", workspace: "T1", name: "maya", email: "maya@example.com", proof: "admin", linked_at: ago(400) }],
  connections: [{ source_id: "s_drive", source: "Shared drive", provider: "google", subject: "maya@example.com", status: "ok", last_ok_at: ago(3) }],
  connectable: [{ source_id: "s_drive", name: "Shared drive", provider: "google" }, { source_id: "s_mail", name: "Support inbox", provider: "google" }],
  personal: { drive: true, calendar: false },
}

const sourceRow = (s: SourceState): DataSource => ({ id: s.id, name: s.name, kind: s.kind, status: s.publication === "retired" ? "retired" : s.status, records: s.records, last_sync_at: s.last_sync_at, fresh: !!s.last_sync_at && s.last_status === "ok" })
const base = "/api/integrations/data"
const admin = "/api/admin/data"

export const dataHandlers: HttpHandler[] = [
  http.get("/api/data/sources", async () => { await wait(); return HttpResponse.json(sources.filter((s) => s.publication === "published" || s.mode === "stored").map(sourceRow)) }),
  http.get("/api/data/search", async ({ request }) => {
    await wait()
    const q = (new URL(request.url).searchParams.get("q") ?? "").toLowerCase()
    const hits: Hit[] = items.filter((i) => i.title.toLowerCase().includes(q)).slice(0, 8).map((i) => ({ record_id: i.id, source_id: i.source_id, kind: i.kind, title: i.title, url: i.url, occurred_at: i.occurred_at, scope: i.scope, snippet: "How a new team member gets access and where the weekly notes live." }))
    const body: Search = { hits, live: [{ source_id: "s_crm", source_name: "Customer notes", hits: [{ title: "Renewal call", kind: "note", url: null, text: "Asked about the annual plan." }], skipped: null }], notes: [] }
    return HttpResponse.json(body)
  }),
  http.get("/api/data/sources/:id/items", async ({ request, params }) => { await wait(); return paged(request, items.filter((i) => i.source_id === params.id)) }),
  http.get("/api/data/records/:id", async ({ params }) => { await wait(); const r = record(String(params.id)); return r ? HttpResponse.json(r) : notFound() }),

  http.get(`${base}/kinds`, () => HttpResponse.json(kinds)),
  http.get(`${base}/sources`, async () => { await wait(); return HttpResponse.json(sources) }),
  http.post(`${base}/sources`, async ({ request }) => {
    const b = (await request.json()) as { kind: string; name: string }
    sources.push({ ...sources[0]!, id: `s_${sources.length + 1}`, name: b.name, kind: b.kind, publication: "shadow", status: "active", records: 0, last_sync_at: null, parity: null, can_publish: false, blocked: "No parity check yet", errors: 0 })
    return HttpResponse.json({}, { status: 201 })
  }),
  http.get(`${base}/sources/:id/runs`, ({ params }) => HttpResponse.json(runs(String(params.id)))),
  http.post(`${base}/sources/:id/:action`, ({ params }) => {
    const s = sources.find((x) => x.id === params.id)
    if (!s) return notFound()
    if (s.bound) return HttpResponse.json({ detail: "This source is declared in the deployment's settings." }, { status: 409 })
    const a = params.action
    if (a === "pause") s.status = "paused"
    if (a === "resume") s.status = "active"
    if (a === "publish") s.publication = "published"
    if (a === "unpublish") s.publication = "shadow"
    if (a === "retire") s.publication = "retired"
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${admin}/people`, ({ request }) => {
    const q = (new URL(request.url).searchParams.get("q") ?? "").toLowerCase()
    return HttpResponse.json(people.filter((p) => p.name.toLowerCase().includes(q)))
  }),
  http.get(`${admin}/scopes`, () => HttpResponse.json(scopes)),
  http.post(`${admin}/scopes`, async ({ request }) => {
    const b = (await request.json()) as { id: string; kind: string; name: string; description: string }
    scopes.push({ ...b, bound: false, holders: [] })
    return HttpResponse.json({}, { status: 201 })
  }),
  http.delete(`${admin}/scopes/:id`, ({ params }) => { const i = scopes.findIndex((s) => s.id === params.id); if (i >= 0) scopes.splice(i, 1); return new HttpResponse(null, { status: 204 }) }),
  http.post(`${admin}/scopes/:id/grants`, async ({ params, request }) => {
    const { holder } = (await request.json()) as { holder: string }
    scopes.find((s) => s.id === params.id)?.holders.push({ holder, name: people.find((p) => `account:${p.account_id}` === holder)?.name ?? null, bound: false })
    return new HttpResponse(null, { status: 204 })
  }),
  http.delete(`${admin}/scopes/:id/grants`, ({ params, request }) => {
    const s = scopes.find((x) => x.id === params.id)
    const holder = new URL(request.url).searchParams.get("holder")
    if (s) s.holders = s.holders.filter((h) => h.holder !== holder)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${admin}/identities`, () => HttpResponse.json(identities)),
  http.get(`${admin}/candidates`, () => HttpResponse.json(candidates)),
  http.post(`${admin}/links`, () => new HttpResponse(null, { status: 204 })),
  http.post(`${admin}/links/reject`, () => new HttpResponse(null, { status: 204 })),
  http.post(`${admin}/links/unlink`, () => new HttpResponse(null, { status: 204 })),
  http.put(`${admin}/people/:id/redacted`, () => new HttpResponse(null, { status: 204 })),
  http.get(`${admin}/redaction`, () => HttpResponse.json(sets)),
  http.get(`${admin}/redaction/:name`, ({ params }) => {
    const info = sets.find((s) => s.name === params.name)
    if (!info) return notFound()
    const body: RedactionDetail = { info, rules, pending_rules: info.pending ? { ...rules, removals: ["internal-only", "draft"] } : null }
    return HttpResponse.json(body)
  }),
  http.post(`${admin}/redaction/:name/approve`, ({ params }) => {
    const s = sets.find((x) => x.name === params.name)
    if (s) { s.pending = false; s.approved_sha = s.pin_sha }
    return new HttpResponse(null, { status: 204 })
  }),

  http.get("/api/profile/data", () => HttpResponse.json({ ...connected, notices: connected.notices.splice(0) })),
  http.post("/api/profile/data/connect", () => HttpResponse.json({ flow: "device", url: "https://example.com/device", pending_id: "p1", user_code: "WXYZ-1234", interval: 5 })),
  http.post("/api/profile/data/connect/:id/poll", () => HttpResponse.json({ connected: false })),
  http.delete("/api/profile/data/connections/:id", () => new HttpResponse(null, { status: 204 })),
  http.put("/api/profile/data/personal/:part", () => HttpResponse.json(connected.personal)),
]

// Mock API for skills mirrored from the data server: the add menu, checks, approvals and per-agent follows.
import { http, HttpResponse } from "msw"

import type { MirrorDoc, ServerSkillOption, SkillFollow } from "@/modules/agents/types"
import { roster } from "./agents"
import { forbidden, notFound, unauthorized, wait } from "./util"

const LABEL = "Data server"
const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const doc = (uri: string, extra: Partial<MirrorDoc> = {}): MirrorDoc => ({
  server: "data", uri, version_label: "1.2", status: "current", pending_digest: null, pending_label: null, pending_changes: null, last_checked_at: ago(0.2), ...extra,
})

/** Mirrors by library skill id; `version` is the library version agents move to. */
const MIRRORS = new Map<string, { doc: MirrorDoc; version: string; description: string }>([
  ["churn-model", { doc: doc("skill://churn-model"), version: "3", description: "Fit and check a hierarchical churn model per cohort." }],
  ["cohort-retention", {
    doc: doc("skill://cohort-retention", { status: "pending", pending_digest: "d-cohort-2", pending_label: "1.3", pending_changes: { added: ["scripts/curves.py"], removed: [], changed: ["SKILL.md"] } }),
    version: "3", description: "Retention curves by signup cohort with credible bands.",
  }],
  ["forecast-baseline", { doc: doc("skill://forecast-baseline", { status: "unavailable", last_checked_at: ago(5) }), version: "3", description: "A seasonal baseline forecast to beat before any model." }],
  ["segment-profiles", { doc: doc("skill://segment-profiles"), version: "3", description: "Profile customer segments from the warehouse tables." }],
  ["survey-weighting", { doc: doc("skill://survey-weighting"), version: "3", description: "Rake survey responses to population margins." }],
  ["geo-lift", { doc: doc("skill://geo-lift"), version: "3", description: "Design and read geo holdout tests." }],
])

const UNMIRRORED: ServerSkillOption[] = [
  { uri: "skill://price-ladders", name: "price-ladders", description: "Build good-better-best price ladders from elasticities.", skill_id: null, taken: false },
  { uri: "skill://attribution-paths", name: "attribution-paths", description: "Multi-touch attribution from journey tables.", skill_id: null, taken: false },
  { uri: "skill://triage", name: "triage", description: "Route data requests to the right analyst.", skill_id: null, taken: true },
]

/** `<agent>:<skill>`: not following, or a held update waiting for the owner. */
const UNFOLLOWED = new Set(["ag_pricing:survey-weighting", "ag_pricing:geo-lift"])
const HELD = new Set(["ag_pricing:segment-profiles"])
/** Agents whose skills settings fix. */
const MANAGED = new Set(["ag_default"])

export const mirrorOf = (id: string): MirrorDoc | null => MIRRORS.get(id)?.doc ?? null

/** Move the agent's pin of `skill` to the mirror's version, as a new agent version. */
function repin(agentId: string, skill: string) {
  const r = roster.find(agentId), m = MIRRORS.get(skill)
  if (!r || !m) return
  const config = structuredClone(r.versions.at(-1)!.config)
  const s = config.skills.find((x) => x.skill_id === skill)
  if (!s || s.version === m.version) return
  s.version = m.version
  r.versions.push({ version: r.versions.length + 1, at: new Date().toISOString(), by: "system", note: `Updated ${s.title}`, config })
  roster.save()
}

const admin = () => {
  const v = roster.viewer()
  return !v ? unauthorized() : !v.admin ? forbidden("The skill library needs an admin.") : null
}

export const serverSkillsHandlers = [
  http.get("/api/agents/server-skills", async () => {
    await wait(300)
    if (!roster.viewer()) return unauthorized()
    const mirrored = [...MIRRORS].map(([id, m]): ServerSkillOption => ({ uri: m.doc.uri, name: id, description: m.description, skill_id: id, taken: false }))
    return HttpResponse.json({ label: LABEL, skills: [...mirrored, ...UNMIRRORED] })
  }),
  http.post("/api/agents/server-skills", async ({ request }) => {
    await wait(500)
    if (!roster.viewer()) return unauthorized()
    const { uri } = (await request.json()) as { uri: string }
    const hit = [...MIRRORS].find(([, m]) => m.doc.uri === uri)
    if (hit) return HttpResponse.json({ id: hit[0], name: hit[0], version: hit[1].version }, { status: 201 })
    const o = UNMIRRORED.find((x) => x.uri === uri)
    if (!o) return notFound()
    if (o.taken) return HttpResponse.json({ detail: `A skill named ${o.name} already exists.` }, { status: 409 })
    return HttpResponse.json({ detail: "This skill could not be read from the server." }, { status: 422 })
  }),
  http.post("/api/agents/skills/:id/check", async ({ params }) => {
    await wait(600)
    const m = MIRRORS.get(String(params.id))
    const denied = admin()
    if (denied) return denied
    if (!m) return notFound()
    m.doc = { ...m.doc, last_checked_at: new Date().toISOString() }
    const result = m.doc.status === "current" ? "unchanged" : m.doc.status
    return HttpResponse.json({ result, server: m.doc })
  }),
  http.post("/api/agents/skills/:id/approve", async ({ params, request }) => {
    await wait(500)
    const id = String(params.id), m = MIRRORS.get(id)
    const denied = admin()
    if (denied) return denied
    if (!m) return notFound()
    const { digest } = (await request.json()) as { digest: string }
    if (m.doc.pending_digest !== digest) return HttpResponse.json({ detail: "No update is waiting." }, { status: 409 })
    m.doc = { ...m.doc, status: "current", version_label: m.doc.pending_label, pending_digest: null, pending_label: null, pending_changes: null }
    return HttpResponse.json({ result: "updated", server: m.doc })
  }),
  http.get("/api/agents/:id/skill-follows", async ({ params }) => {
    await wait(150)
    const agent = String(params.id)
    if (!roster.viewer()) return unauthorized()
    if (!roster.find(agent)) return notFound()
    const out: Record<string, SkillFollow> = {}
    for (const [id, m] of MIRRORS) {
      const key = `${agent}:${id}`, managed = MANAGED.has(agent), follow = !UNFOLLOWED.has(key)
      out[id] = {
        follow: follow && !managed, version: m.version,
        status: managed ? "managed" : follow && HELD.has(key) ? "needs_reconfirm" : m.doc.status,
      }
    }
    return HttpResponse.json(out)
  }),
  http.put("/api/agents/:id/skill-follows", async ({ params, request }) => {
    await wait(300)
    const agent = String(params.id)
    if (!roster.viewer()) return unauthorized()
    if (MANAGED.has(agent)) return forbidden("Managed by settings.")
    const { skill_id, follow } = (await request.json()) as { skill_id: string; follow: boolean }
    const key = `${agent}:${skill_id}`
    if (follow) UNFOLLOWED.delete(key)
    else UNFOLLOWED.add(key)
    // Following again moves the agent at once, unless its backend consent needs the owner.
    if (follow && !HELD.has(key) && MIRRORS.get(skill_id)?.doc.status === "current") repin(agent, skill_id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.post("/api/agents/:id/skill-follows/:skill/apply", async ({ params }) => {
    await wait(400)
    const agent = String(params.id), skill = String(params.skill), key = `${agent}:${skill}`
    if (!roster.viewer()) return unauthorized()
    if (UNFOLLOWED.has(key) || !HELD.has(key)) return HttpResponse.json({ detail: "No update is waiting." }, { status: 409 })
    HELD.delete(key)
    repin(agent, skill)
    return new HttpResponse(null, { status: 204 })
  }),
]

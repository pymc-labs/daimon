// Artifacts: pages agents publish, their shared data, events and versions.
import { http, HttpResponse, type HttpHandler } from "msw"

import type { Msg } from "@/modules/chat/types"
import { publicRefusal } from "@/modules/artifacts/rules"
import type {
  ArtifactChange, ArtifactDetail, ArtifactEvent, ArtifactSummary, ArtifactVersion, DataCollection, DataDocument, PersonRef,
} from "@/modules/artifacts/types"
import { forbidden, notFound, unauthorized, wait } from "./util"
import { agent, me, PEOPLE } from "./world"

const DAY = 86_400_000
const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const inDays = (d: number) => new Date(Date.now() + d * DAY).toISOString()
const ORIGIN = "https://daimon.fernhill.example"

const EXTRA: PersonRef[] = [
  { id: "p_marco", name: "Marco Alvarez", kind: "member" }, { id: "p_sofia", name: "Sofia Brandt", kind: "member" },
  { id: "p_tom", name: "Tom Okafor", kind: "member" },
]
const PEOPLE_REFS: PersonRef[] = PEOPLE.map((p) => ({ id: p.id, name: p.name, kind: p.role === "visitor" ? "visitor" : "member" }))
export const ref = (id: string) => [...PEOPLE_REFS, ...EXTRA].find((p) => p.id === id)
const MAYA = ref("p_maya")!, JONAS = ref("p_jonas")!, PRIYA = ref("p_priya")!, JORDAN = ref("p_jordan")!
const MARCO = ref("p_marco")!, SOFIA = ref("p_sofia")!, TOM = ref("p_tom")!
const PEOPLE_ALL = [MAYA, JONAS, PRIYA, MARCO, SOFIA, TOM, JORDAN]

type Docs = Record<string, DataDocument[]>
type Stored = Omit<ArtifactDetail, "mine" | "role" | "people_count" | "can" | "usage" | "url"> & { docs: Docs; events_log: ArtifactEvent[]; versions: ArtifactVersion[] }

const doc = (id: string, data: unknown, h: number, by: string): DataDocument => ({ id, data, updated_at: ago(h), updated_by: by, size_bytes: JSON.stringify(data).length })
const ev = (event: string, data: unknown, by: PersonRef | null, sender: ArtifactEvent["sender"], h: number, delivered = 1): ArtifactEvent =>
  ({ id: `ev_${Math.abs(Math.round(h * 1000))}_${event}`, event, data, by: by?.name ?? null, sender, at: ago(h), delivered })
const ag = (id: string) => { const a = agent(id); return { id: a.id, name: a.name, avatar: a.avatar } }

function versions(n: number, published: string, by: PersonRef[], agentName: string, hours: number, chat: Record<number, string> = {}): ArtifactVersion[] {
  return Array.from({ length: Math.min(n, 20) }, (_, i) => {
    const v = n - i
    return { version: v, created_at: ago(hours + i * 31 + (i ? i * 7 : 0)), size_bytes: Math.round(published.length * (0.55 + 0.45 * (v / n))) , by: by[i % by.length]!.name, agent: agentName, current: i === 0, chat_url: chat[v] ?? null }
  })
}

type Seed = Pick<Stored, "id" | "slug" | "title" | "agent" | "owner" | "access" | "data" | "events" | "ask" | "version" | "created_at" | "updated_at" | "expires_at">
  & Partial<Stored> & { size: number; versionsBy: PersonRef[]; chat?: Record<number, string> }
const art = ({ size, versionsBy, chat, ...a }: Seed): Stored => ({
  link: null, docs: {}, events_log: [], watches: [], asks: { threads: 0, turns_7d: 0 }, editors: [], people: [], editors_can_share: true, data_scale: 1,
  ...a, size_bytes: size,
  versions: versions(a.version, "x".repeat(size), versionsBy, a.agent.name, (Date.now() - new Date(a.updated_at).getTime()) / 3_600_000, chat),
})

const SECRET = "Vq7mT2xK9dRb4HnY"
const artifacts: Stored[] = [
  art({
    id: "k4rm7q2xdn", slug: "content-run-sheet", title: "Content run sheet", agent: ag("ag_default"), owner: MAYA, access: "workspace",
    data: "write", events: true, ask: true, version: 7, size: 38_400, created_at: ago(24 * 19), updated_at: ago(2.5), expires_at: null,
    editors: [JONAS], editors_can_share: true, versionsBy: [MAYA, JONAS], chat: { 7: "/chat/s_runsheet", 5: "/chat/s_runsheet" },
    docs: {
      runs: [
        doc("run_0041", { title: "Spring launch teaser", channel: "LinkedIn", stage: "Plan", owner: "Sofia Brandt", due: "2026-11-27" }, 30, "Sofia Brandt"),
        doc("run_0040", { title: "Larkspur Q3 case study", channel: "Blog", stage: "Draft", owner: "Priya Raman", due: "2026-11-25" }, 5, "Priya Raman"),
        doc("run_0039", { title: "Elasticity explainer thread", channel: "X", stage: "Review", owner: "Maya Lindqvist", due: "2026-11-24" }, 3, "Maya Lindqvist"),
        doc("run_0038", { title: "Webinar recap email", channel: "Email", stage: "Review", owner: "Jonas Weber", due: "2026-11-23" }, 9, "Jonas Weber"),
        doc("run_0037", { title: "Almanac changelog, November", channel: "Blog", stage: "Done", owner: "Tom Okafor", due: "2026-11-18" }, 70, "Tom Okafor"),
        doc("run_0036", { title: "Cobalt churn teaser", channel: "LinkedIn", stage: "Done", owner: "Marco Alvarez", due: "2026-11-14" }, 120, "Marco Alvarez"),
      ],
      requests: [
        doc("req_118", { text: "Add a podcast clip run for the pricing episode", by: "Tom Okafor", status: "open" }, 4, "Tom Okafor"),
        doc("req_117", { text: "Move the webinar recap to Wednesday", by: "Jonas Weber", status: "done" }, 28, "Jonas Weber"),
        doc("req_116", { text: "Need a LinkedIn carousel for the Larkspur result", by: "Priya Raman", status: "open" }, 52, "Priya Raman"),
      ],
    },
    events_log: [
      ev("run_requested", { run: "run_0041", title: "Spring launch teaser" }, SOFIA, "member", 30),
      ev("stage_changed", { run: "run_0039", from: "Draft", to: "Review" }, MAYA, "member", 3, 2),
      ev("request_added", { request: "req_118" }, TOM, "member", 4),
      ev("stage_changed", { run: "run_0038", from: "Plan", to: "Review" }, JONAS, "member", 9, 2),
      ev("run_requested", { run: "run_0040", title: "Larkspur Q3 case study" }, PRIYA, "member", 52),
    ],
    watches: [
      { id: "w_req", name: "New run requests", owner: "Maya Lindqvist", agent: "Daimon", status: "active", turns_today: 3, max_turns_per_day: 50, thread_url: "/chat/s_runsheet" },
      { id: "w_stage", name: "Stage changes to Review", owner: "Jonas Weber", agent: "Daimon", status: "paused", turns_today: 0, max_turns_per_day: 20, thread_url: null },
    ],
    asks: { threads: 4, turns_7d: 17 },
  }),
  art({
    id: "w3tb5hs6ec", slug: "q3-pricing-review", title: "Q3 pricing review", agent: ag("ag_pricing"), owner: PRIYA, access: "public",
    data: "off", events: false, ask: false, version: 4, size: 112_300, created_at: ago(24 * 12), updated_at: ago(26), expires_at: inDays(21),
    link: `${ORIGIN}/a/w3tb5hs6ec/q3-pricing-review?k=${SECRET}`, editors: [MAYA], versionsBy: [PRIYA, PRIYA, MAYA, PRIYA],
  }),
  art({
    id: "p7n2jv4gfa", slug: "onboarding-checklist", title: "Onboarding checklist", agent: ag("ag_default"), owner: MAYA, access: "people",
    data: "write", events: false, ask: false, version: 3, size: 21_700, created_at: ago(24 * 8), updated_at: ago(48), expires_at: null,
    people: [MARCO, SOFIA, TOM], editors: [JONAS, PRIYA], editors_can_share: false, versionsBy: [MAYA, JONAS],
    docs: {
      people: [
        doc("marco", { name: "Marco Alvarez", role: "Data analyst", start: "2026-11-24", buddy: "Priya Raman" }, 40, "Maya Lindqvist"),
        doc("sofia", { name: "Sofia Brandt", role: "Content lead", start: "2026-12-01", buddy: "Maya Lindqvist" }, 60, "Maya Lindqvist"),
      ],
      tasks: [
        doc("marco_laptop", { person: "marco", task: "Laptop and accounts", done: true }, 38, "Jonas Weber"),
        doc("marco_access", { person: "marco", task: "Warehouse read access", done: true }, 30, "Jonas Weber"),
        doc("marco_intro", { person: "marco", task: "Intro call with the MMM team", done: false }, 40, "Marco Alvarez"),
        doc("marco_repo", { person: "marco", task: "Clone the models repo", done: false }, 40, "Marco Alvarez"),
      ],
    },
  }),
  art({
    id: "h6cz3ydm5u", slug: "weekly-metrics", title: "Weekly metrics", agent: ag("ag_mmm"), owner: JONAS, access: "workspace",
    data: "read", events: false, ask: false, version: 12, size: 64_900, created_at: ago(24 * 60), updated_at: ago(5), expires_at: null, versionsBy: [JONAS],
    docs: { metrics: [doc("2026-w46", { week: "2026-W46", spend_usd: 142600, roas: 2.9, turns: 298 }, 5, "MMM Analyst"), doc("2026-w45", { week: "2026-W45", spend_usd: 162100, roas: 2.7, turns: 311 }, 173, "MMM Analyst")] },
  }),
  art({
    id: "e2xq7aw4tr", slug: "hiring-scorecard", title: "Hiring scorecard", agent: ag("ag_default"), owner: MAYA, access: "people",
    data: "write", events: true, ask: false, version: 3, size: 29_100, created_at: ago(24 * 6), updated_at: ago(96), expires_at: inDays(60),
    people: [JONAS, PRIYA, SOFIA], editors: [JONAS], versionsBy: [MAYA],
    docs: {
      candidates: [doc("c_ines", { name: "Ines Carvalho", role: "Data scientist", stage: "Onsite" }, 80, "Maya Lindqvist"), doc("c_omar", { name: "Omar Haddad", role: "Data scientist", stage: "Screen" }, 90, "Maya Lindqvist")],
      scores: [doc("s_ines_jonas", { candidate: "c_ines", by: "Jonas Weber", modeling: 5, communication: 4 }, 70, "Jonas Weber")],
    },
    events_log: [ev("score_submitted", { candidate: "c_ines", scorer: "jonas" }, JONAS, "member", 70), ev("score_submitted", { candidate: "c_ines", scorer: "priya" }, PRIYA, "member", 66)],
    watches: [{ id: "w_score", name: "Scores submitted", owner: "Maya Lindqvist", agent: "Daimon", status: "active", turns_today: 0, max_turns_per_day: 20, thread_url: null }],
  }),
  art({
    id: "s5fb6mk3ya", slug: "launch-faq", title: "Launch FAQ", agent: ag("ag_sales"), owner: PRIYA, access: "workspace",
    data: "off", events: false, ask: false, version: 2, size: 14_200, created_at: ago(24 * 3), updated_at: ago(24 * 3), expires_at: null, versionsBy: [PRIYA],
  }),
]

// The thread that published the run sheet, and the ask thread the page opened.
export function artifactThread(sid: string): Msg[] | null {
  const at = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
  const card = { id: "k4rm7q2xdn", title: "Content run sheet", url: "/a/k4rm7q2xdn/content-run-sheet", version: 7, access: "workspace" as const }
  if (sid === "s_runsheet") {
    return [
      { id: "m_rs_1", role: "user", content: "Build a run sheet for the content team. Runs move through Plan, Draft, Review and Done, and anyone can request a new run.", created_at: at(200) },
      {
        id: "m_rs_2", role: "assistant", created_at: at(150), cost_usd: 0.31, ms: 21_400, feedback: null, artifacts: [card],
        content: "Published the **Content run sheet**. Runs are listed by stage and anyone in the workspace can request one.\n\n- Runs and requests are stored in the page's shared data.\n- A new request notifies the \"New run requests\" watch, which opens a thread for you.\n- The Ask box starts a thread of its own for whoever types in it.",
        steps: [{ id: "a", label: "Wrote page", detail: "content-run-sheet, 38 KB", status: "done", ms: 5_200 }, { id: "b", label: "Published artifact", detail: "version 7", status: "done", ms: 640 }],
      },
    ]
  }
  if (sid === "s_ask_runsheet") {
    return [
      { id: "m_ask_1", role: "user", content: "Which runs are waiting on review?", created_at: at(12) },
      {
        id: "m_ask_2", role: "assistant", created_at: at(11), cost_usd: 0.04, ms: 4_100, feedback: null,
        content: "Two runs are in Review:\n\n| Run | Channel | Owner | Due |\n|---|---|---|---|\n| Elasticity explainer thread | X | Maya Lindqvist | 24 Nov |\n| Webinar recap email | Email | Jonas Weber | 23 Nov |",
        steps: [{ id: "a", label: "Read page data", detail: "runs, 6 documents", status: "done", ms: 380 }],
      },
    ]
  }
  return null
}

// ---- views

const people_of = (ids: string[]) => ids.map((id) => ref(id)).filter((p): p is PersonRef => !!p)
const isAdmin = (r: string) => r === "owner" || r === "admin"
function roleOf(a: Stored): ArtifactSummary["role"] {
  const m = me()!
  return a.owner.id === m.id ? "owner" : a.editors.some((e) => e.id === m.id) ? "editor" : isAdmin(m.role) ? "admin" : "viewer"
}
const can = (a: Stored): ArtifactDetail["can"] => {
  const r = roleOf(a)
  const full = r === "owner" || r === "admin"
  return { edit: r !== "viewer", share: full || (r === "editor" && a.editors_can_share), delete: full, transfer: full }
}
const visible = (a: Stored) => {
  const m = me()
  if (!m || m.role === "visitor") return false
  // A public page is listed only to those who manage it.
  return roleOf(a) !== "viewer" || a.access === "workspace" || (a.access === "people" && a.people.some((p) => p.id === m.id))
}
const docsOf = (a: Stored) => Object.values(a.docs).flat()
function summary(a: Stored): ArtifactSummary {
  return {
    id: a.id, slug: a.slug, title: a.title, url: `/a/${a.id}/${a.slug}`, agent: a.agent, owner: a.owner, mine: a.owner.id === me()!.id, role: roleOf(a),
    access: a.access, people_count: a.people.length, data: a.data, events: a.events, ask: a.ask,
    version: a.version, size_bytes: a.size_bytes, updated_at: a.updated_at, created_at: a.created_at, expires_at: a.expires_at,
  }
}
function detail(a: Stored): ArtifactDetail {
  const all = docsOf(a)
  return {
    ...summary(a), people: a.people, editors: a.editors, editors_can_share: a.editors_can_share, data_scale: a.data_scale, link: a.link,
    watches: a.watches, asks: a.asks, can: can(a),
    usage: { documents: all.length, max_documents: 5_000 * a.data_scale, bytes: all.reduce((n, d) => n + d.size_bytes, 0), max_bytes: 20 * 1_048_576 * a.data_scale },
  }
}
const invalid = (detail: string) => HttpResponse.json({ detail }, { status: 422 })
const find = (id: unknown) => artifacts.find((a) => a.id === id && visible(a))
const secret = () => Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 10)

function bump(a: Stored, size: number, by: string, chat: string | null = null) {
  a.version += 1
  a.size_bytes = size
  a.updated_at = new Date().toISOString()
  a.versions.forEach((v) => (v.current = false))
  a.versions.unshift({ version: a.version, created_at: a.updated_at, size_bytes: size, by, agent: a.agent.name, current: true, chat_url: chat })
  a.versions.length = Math.min(a.versions.length, 20)
}

const guard = (id: unknown, need?: keyof ArtifactDetail["can"]) => {
  if (!me()) return { res: unauthorized() }
  const a = find(id)
  if (!a) return { res: notFound() }
  if (need && !can(a)[need]) return { res: forbidden("Ask the owner or an admin.") }
  return { a }
}

export const artifactsHandlers: HttpHandler[] = [
  http.get("/api/artifacts", async () => {
    await wait()
    if (!me()) return unauthorized()
    return HttpResponse.json(artifacts.filter(visible).map(summary).sort((x, y) => y.updated_at.localeCompare(x.updated_at)))
  }),
  // People the pickers offer; not part of the listed contract.
  http.get("/api/artifacts/people", async ({ request }) => {
    await wait(80)
    const q = (new URL(request.url).searchParams.get("q") ?? "").toLowerCase()
    return HttpResponse.json(PEOPLE_ALL.filter((p) => p.name.toLowerCase().includes(q)))
  }),
  http.get("/api/artifacts/:id", async ({ params }) => {
    await wait()
    const { a, res } = guard(params.id)
    return a ? HttpResponse.json(detail(a)) : res
  }),
  http.patch("/api/artifacts/:id", async ({ params, request }) => {
    await wait(220)
    const { a, res } = guard(params.id, "edit")
    if (!a) return res
    const b = (await request.json()) as ArtifactChange
    const sharing = ["access", "people", "editors", "editors_can_share", "expires_at"].some((k) => k in b)
    if (sharing && !can(a).share) return forbidden("Ask the owner or an admin.")
    const next = { access: b.access ?? a.access, data: b.data ?? a.data, events: b.events ?? a.events, ask: b.ask ?? a.ask }
    if (next.access === "public") {
      const why = publicRefusal(next)
      if (why) return invalid(why)
    }
    if (b.data_scale !== undefined && !(b.data_scale >= 1 && b.data_scale <= 10)) return invalid("Data space is 1x to 10x.")
    const lists = { people: b.people && people_of(b.people), editors: b.editors && people_of(b.editors) }
    if ((b.people && lists.people!.length !== b.people.length) || (b.editors && lists.editors!.length !== b.editors.length)) return invalid("Pick people from the list.")
    if (b.title !== undefined) a.title = b.title.trim() || a.title
    if (lists.people) a.people = lists.people
    if (lists.editors) a.editors = lists.editors
    if (b.editors_can_share !== undefined) a.editors_can_share = b.editors_can_share
    if (b.data_scale !== undefined) a.data_scale = b.data_scale
    if (b.expires_at !== undefined) a.expires_at = b.expires_at
    else if (next.access === "public" && a.access !== "public" && !a.expires_at) a.expires_at = inDays(30)
    a.link = next.access === "public" ? (a.link ?? `${ORIGIN}/a/${a.id}/${a.slug}?k=${secret()}`) : null
    Object.assign(a, next)
    return HttpResponse.json(detail(a))
  }),
  http.delete("/api/artifacts/:id", async ({ params }) => {
    await wait(200)
    const { a, res } = guard(params.id, "delete")
    if (!a) return res
    artifacts.splice(artifacts.indexOf(a), 1)
    return new HttpResponse(null, { status: 204 })
  }),
  http.post("/api/artifacts/:id/link", async ({ params }) => {
    await wait(200)
    const { a, res } = guard(params.id, "share")
    if (!a) return res
    if (a.access !== "public") return invalid("Only a public page has a link.")
    a.link = `${ORIGIN}/a/${a.id}/${a.slug}?k=${secret()}`
    return HttpResponse.json(detail(a))
  }),
  http.post("/api/artifacts/:id/transfer", async ({ params, request }) => {
    await wait(200)
    const { a, res } = guard(params.id, "transfer")
    if (!a) return res
    const { person } = (await request.json()) as { person: string }
    const to = a.editors.find((e) => e.id === person)
    if (!to) return invalid("Pick one of the editors.")
    a.editors = [a.owner, ...a.editors.filter((e) => e.id !== to.id)]
    a.owner = to
    return HttpResponse.json(detail(a))
  }),
  http.get("/api/artifacts/:id/versions", async ({ params }) => {
    await wait()
    const { a, res } = guard(params.id)
    return a ? HttpResponse.json(a.versions) : res
  }),
  http.post("/api/artifacts/:id/versions/:v/restore", async ({ params }) => {
    await wait(250)
    const { a, res } = guard(params.id, "edit")
    if (!a) return res
    const v = a.versions.find((x) => x.version === Number(params.v))
    if (!v || v.current) return invalid("Pick an older version.")
    bump(a, v.size_bytes, me()!.name)
    return HttpResponse.json(detail(a))
  }),
  http.get("/api/artifacts/:id/data", async ({ params }) => {
    await wait()
    const { a, res } = guard(params.id)
    if (!a) return res
    const out: DataCollection[] = Object.entries(a.docs).map(([name, d]) => ({ name, documents: d.length, bytes: d.reduce((n, x) => n + x.size_bytes, 0) }))
    return HttpResponse.json(out)
  }),
  http.get("/api/artifacts/:id/data/:collection", async ({ params }) => {
    await wait()
    const { a, res } = guard(params.id)
    const rows = a?.docs[String(params.collection)]
    return a ? (rows ? HttpResponse.json(rows) : notFound()) : res
  }),
  http.put("/api/artifacts/:id/data/:collection/:doc", async ({ params, request }) => {
    await wait(200)
    const { a, res } = guard(params.id, "edit")
    if (!a) return res
    const { data } = (await request.json()) as { data: unknown }
    const rows = (a.docs[String(params.collection)] ??= [])
    const next = { id: String(params.doc), data, updated_at: new Date().toISOString(), updated_by: me()!.name, size_bytes: JSON.stringify(data).length }
    const i = rows.findIndex((d) => d.id === next.id)
    if (i >= 0) rows[i] = next
    else rows.unshift(next)
    return HttpResponse.json(next)
  }),
  http.delete("/api/artifacts/:id/data/:collection/:doc", async ({ params }) => {
    await wait(200)
    const { a, res } = guard(params.id, "edit")
    if (!a) return res
    const c = String(params.collection)
    a.docs[c] = (a.docs[c] ?? []).filter((d) => d.id !== params.doc)
    if (!a.docs[c]!.length) delete a.docs[c]
    return new HttpResponse(null, { status: 204 })
  }),
  http.get("/api/artifacts/:id/events", async ({ params }) => {
    await wait()
    const { a, res } = guard(params.id)
    return a ? HttpResponse.json(a.events_log) : res
  }),
]

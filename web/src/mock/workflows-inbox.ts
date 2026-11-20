// Publish and approval: requests for grants, the inbox, and versions you can open, compare and restore.
import { http, HttpResponse, type HttpHandler } from "msw"

import { newGrants } from "@/modules/workflows/review"
import { clone } from "@/modules/workflows/spec"
import type {
  Decision, Draft, InboxDecide, InboxDetail, InboxItem, InboxRoute, Person, Spec, Version, VersionSpec,
} from "@/modules/workflows/types"
import { forbidden, notFound, unauthorized, wait } from "./util"
import { me } from "./world"
import { ago, allRuns, detail, draftOf, find, findRun, mkDraft, P, versionsOf, W, type Stored } from "./workflows"
import { decideWait } from "./workflows-runs"
import { publicRequest, requests } from "./workflows-store"

const who = (): Person => ({ id: me()!.id, name: me()!.name })

// ---------- publish ----------

/** Makes a spec the live version: nothing in flight is touched. */
function publishNow(w: Stored, spec: Spec, by: Person, note: string, approvedBy: Person | null) {
  w.live = spec
  w.version = (w.version ?? 0) + 1
  w.draft = null
  if (w.status === "draft") w.status = "live"
  w.published = { version: w.version, by, at: new Date().toISOString(), note, approved_by: approvedBy }
}

const unknown = (id: unknown) => HttpResponse.json({ detail: `No such ${String(id)}` }, { status: 404 })

// ---------- inbox ----------

type Rec = Omit<InboxItem, "decision"> & {
  to: string[]; show?: string; bound?: InboxDetail["bound"]; proposals?: InboxDetail["proposals"]; question?: string
  form?: InboxDetail["form"]; values?: Record<string, unknown>; sent?: InboxDetail["sent"]; attachments?: InboxDetail["attachments"]; answer?: InboxDetail["answer"]; routes: InboxRoute[]; decision: Decision | null; request?: string
}
const approve: InboxRoute[] = [{ name: "reject", label: "Reject", primary: false, comment: true }, { name: "approve", label: "Approve", primary: true, comment: false }]
const run = (wf: string, n: number) => ({ id: `${wf}_run${n}`, number: n })

const LEAD_BODY = "Hi Ana,\n\nThanks for reaching out. Forecasting promotions across 40 stores is what our MMM work is built for, and Brightwave's Q1 calendar is a good fit.\n\nCould we find 30 minutes next week? I can share two example readouts beforehand.\n\nBest,\nMaya"
const ARTICLE = "Priors are where a media mix model says what you already know. A weak prior on adstock decay lets the data speak, but with 18 months of weekly spend it mostly speaks noise.\n\nWe set decay priors from channel type, then let the likelihood move them. Paid search decays fast, TV slowly, and the posterior shows how far each moved."

const RECS: Rec[] = [
  { id: "ib_lead", kind: "approval", title: "Approve reply", workflow: { id: "wf_lead", name: "Inbound lead" }, run: run("wf_lead", 128), step: "review", asked_by: "Sales Desk",
    since: ago(2.2), expires_at: ago(-46), to: ["p_maya"], decision: null, routes: approve,
    show: "**Brightwave Telecom** (strong fit)\n" + LEAD_BODY,
    bound: [{ tool: "gmail/send_message", label: "Send email", step: "Send", args: { to: "ana@brightwave.example", subject: "Forecasting promotions before Q1", body: LEAD_BODY } }],
    form: { type: "object", required: ["body"], properties: { body: { type: "string", title: "Reply" } } }, values: { body: LEAD_BODY } },
  { id: "ib_content", kind: "approval", title: "Editor approval", workflow: { id: "wf_content", name: "Content pipeline" }, run: run("wf_content", 125), step: "approve", asked_by: "Content Writer",
    since: ago(7), expires_at: ago(-65), to: ["p_maya", "p_priya"], decision: null, routes: approve, show: "**Priors for media mix**\n" + ARTICLE,
    bound: [{ tool: "notion/create_page", label: "Create page", step: "Create Notion page", args: { title: "Priors for media mix", parent: "Blog drafts" } }] },
  { id: "ib_triage", kind: "proposals", title: "Label issues", workflow: { id: "wf_triage", name: "Issue triage" }, run: run("wf_triage", 126), step: "label", asked_by: "Code Reviewer",
    since: ago(4.5), expires_at: ago(-19), to: ["p_jonas", "p_priya"], decision: null,
    routes: [{ name: "reject", label: "Reject all", primary: false, comment: true }, { name: "approve", label: "Send selected", primary: true, comment: false }],
    proposals: [
      { id: "pr1", tool: "github/create_comment", title: "Adstock decay ignores lag", detail: "Label bug and add: Reproduced on 0.9. The lag term never reaches the decay kernel." },
      { id: "pr2", tool: "github/create_comment", title: "Docs: install on Windows", detail: "Label docs and add: Thanks, the conda steps on the page are out of date." },
      { id: "pr3", tool: "github/create_comment", title: "Support Python 3.14", detail: "Label enhancement and add: We test 3.13 today. A PR adding 3.14 to CI is welcome." },
      { id: "pr4", tool: "github/create_comment", title: "Prior predictive fails", detail: "Label question and add: Could you share the model and seed? It runs on our fixtures." },
    ] },
  { id: "ib_onboard", kind: "question", title: "Which channel for the kickoff", workflow: { id: "wf_onboard", name: "Client onboarding" }, run: run("wf_onboard", 124), step: "welcome", asked_by: "Sales Desk",
    since: ago(1.4), expires_at: null, to: ["p_maya", "p_jonas"], decision: null,
    routes: [{ name: "answer", label: "Send answer", primary: true, comment: false }], question: "Where should the Larkspur Retail kickoff notes go?",
    form: { type: "object", required: ["channel"], properties: { channel: { type: "string", title: "Channel", enum: ["#delivery", "#clients", "#leads"] }, note: { type: "string", title: "Note" } } },
    values: { channel: "#delivery" },
    sent: [{ platform: "discord", where: "Direct message to Maya Lindqvist", link: "https://discord.com/channels/745261709622771773/1/2", at: ago(1.4) }],
    attachments: [{ name: "kickoff-notes.md", size: 2140, url: "/api/workflows/runs/wf_onboard_run124/files/kickoff-notes.md" }] },
  { id: "ib_alert", kind: "approval", title: "Page the on-call", workflow: { id: "wf_alert", name: "Alert triage" }, run: run("wf_alert", 128), step: "page", asked_by: "Daimon",
    since: ago(0.4), expires_at: ago(-0.2), to: ["p_jonas"], decision: null, routes: approve,
    show: "**api-latency-p99** above 900 ms for 12 minutes.\nLikely cause: the new rate limiter on `/v1/score`. Paging the on-call with the graph and the deploy link.",
    bound: [{ tool: "slack/post_message", label: "Post message", step: "Page", args: { channel: "#oncall", text: "api-latency-p99 above 900 ms for 12 minutes. Likely the rate limiter deploy from 14:10." } }] },
  { id: "ib_digest", kind: "approval", title: "Check the digest", workflow: { id: "wf_digest", name: "Weekly pipeline digest" }, run: run("wf_digest", 128), step: "review", asked_by: "Sales Desk",
    since: ago(30), expires_at: null, to: ["p_maya", "p_jonas"], routes: approve,
    decision: { route: "approve", label: "Approved", by: P.maya!, at: ago(29), comment: null, selected: null }, show: "**3 risky deals** this week, 2 with a slipped close date.\nNorthwind renewal has had no activity in 21 days.",
    bound: [{ tool: "slack/post_message", label: "Post message", step: "Post to sales", args: { channel: "#sales", text: "3 risky deals this week." } }] },
]
const VERB: Record<string, string> = { approve: "Approved", reject: "Rejected", answer: "Answered" }
const live = new Map<string, Rec>(RECS.map((r) => [r.id, r]))

function fromRequest(r: ReturnType<typeof requests.all>[number]): Rec {
  const grants = r.grants.length
  return {
    id: `ib_${r.id}`, kind: "publish", title: `Publish v${r.version} of ${r.workflow.name}`, workflow: r.workflow, run: null, step: null, asked_by: r.by.name, since: r.at, expires_at: null,
    to: ["admin"], request: r.id, routes: [{ name: "reject", label: "Reject", primary: false, comment: true }, { name: "approve", label: grants ? "Approve and publish" : "Approve", primary: true, comment: false }],
    decision: r.status === "pending" ? null : { route: r.status === "approved" ? "approve" : "reject", label: r.status === "approved" ? "Approved" : "Rejected", by: r.decided_by!, at: r.decided_at!, comment: r.comment, selected: null },
  }
}

function recsFor(): Rec[] {
  const m = me()!
  const mine = RECS.filter((r) => r.to.includes(m.id))
  const reqs = m.role === "admin" ? requests.all().map(fromRequest) : []
  return [...reqs, ...mine].sort((a, b) => b.since.localeCompare(a.since))
}
const recOf = (id: string): Rec | undefined => recsFor().find((r) => r.id === id)

const item = ({ to: _to, show: _s, bound: _b, proposals: _p, question: _q, form: _f, values: _v, sent: _sn, attachments: _at, answer: _an, routes: _r, request: _x, ...i }: Rec): InboxItem => i

function detailOf(r: Rec): InboxDetail {
  const req = r.request ? requests.find(r.request) : undefined
  return {
    ...item(r), show: r.show ?? null, bound: r.bound ?? [], proposals: r.proposals ?? [], question: r.question ?? null, form: r.form ?? null, values: r.values ?? {}, sent: r.sent ?? [], attachments: r.attachments ?? [], answer: r.answer ?? null,
    routes: r.routes, publish: req ? publicRequest(req) : null,
  }
}

// ---------- versions ----------

/** Earlier versions: the live spec with its recent edits taken back, one step per version. */
function specAt(w: Stored, n: number): Spec | null {
  if (!w.live || !w.version || n < 1 || n > w.version) return null
  const s = clone(w.live)
  const back = w.version - n
  const first = Object.values(s.nodes).find((x) => x.kind === "agent")
  if (back >= 1 && first?.instructions) first.instructions = first.instructions.split(/(?<=\.)\s/)[0]!.trim() + "\n"
  if (back >= 2) {
    const t = s.triggers?.[0]
    if (t?.kind === "schedule") t.cron = "0 10 * * 1"
    else if (t?.kind === "webhook") t.respond = "result"
    else s.goal = (s.goal ?? "").split(". ")[0]
  }
  if (back >= 3) s.defaults = { ...s.defaults, budget_usd: 0.5 }
  if (back >= 4) delete s.limits
  if (back >= 5) s.visibility = "private"
  return s
}
const versionOf = (w: Stored, n: number): Version | undefined => versionsOf(w).find((v) => v.number === n)

export const workflowsInboxHandlers: HttpHandler[] = [
  http.post("/api/workflows/:id/draft/publish", async ({ params, request }) => {
    await wait(600)
    const w = find(params.id)
    if (!w?.draft) return notFound()
    if (w.draft.validation.errors.length) return HttpResponse.json({ detail: "Fix the problems first." }, { status: 422 })
    const { note } = (await request.json().catch(() => ({ note: "" }))) as { note: string }
    if (newGrants(w.live, w.draft.spec).length && me()!.role !== "admin") return forbidden("New grants need an admin. Request approval instead.")
    publishNow(w, w.draft.spec, who(), note, null)
    return HttpResponse.json(detail(w))
  }),
  http.post("/api/workflows/:id/draft/request", async ({ params, request }) => {
    await wait(500)
    const w = find(params.id)
    if (!w?.draft) return notFound()
    if (w.draft.validation.errors.length) return HttpResponse.json({ detail: "Fix the problems first." }, { status: 422 })
    const { note } = (await request.json().catch(() => ({ note: "" }))) as { note: string }
    requests.add({ workflow: { id: w.id, name: w.draft.spec.name }, version: (w.version ?? 0) + 1, note, by: who(), revision: w.draft.revision, grants: newGrants(w.live, w.draft.spec), spec: clone(w.draft.spec) })
    return HttpResponse.json(detail(w), { status: 201 })
  }),
  http.get("/api/workflows/inbox", async () => {
    await wait()
    return me() ? HttpResponse.json(recsFor().map(item)) : unauthorized()
  }),
  http.get("/api/workflows/inbox/:id", async ({ params }) => {
    await wait(120)
    const r = recOf(String(params.id))
    return r ? HttpResponse.json(detailOf(r)) : notFound()
  }),
  http.post("/api/workflows/inbox/:id/decide", async ({ params, request }) => {
    await wait(400)
    const b0 = (await request.clone().json()) as InboxDecide
    const wr = allRuns().find((x) => String(params.id).startsWith(`wt_${x.id}_`))
    if (wr) {
      const key = String(params.id).slice(`wt_${wr.id}_`.length)
      const text = typeof b0.values?.text === "string" ? b0.values.text : undefined
      const by = who()
      if (!decideWait(wr, key, b0.route, by, Date.now(), text)) return HttpResponse.json({ detail: "This step is no longer waiting." }, { status: 409 })
      return HttpResponse.json({ id: String(params.id), decision: { route: b0.route, label: VERB[b0.route] ?? "Decided", by, at: new Date().toISOString(), comment: null, selected: null } })
    }
    const r = recOf(String(params.id))
    if (!r) return notFound()
    if (r.decision) return HttpResponse.json({ detail: `Already decided by ${r.decision.by.name}.` }, { status: 409 })
    const b = (await request.json()) as InboxDecide
    const route = r.routes.find((x) => x.name === b.route)
    if (!route) return unknown(b.route)
    if (route.comment && !b.comment?.trim()) return HttpResponse.json({ detail: "Add a comment." }, { status: 422 })
    const by = who()
    if (r.request) {
      const req = requests.find(r.request)!
      if (b.route === "approve") {
        const w = W.find((x) => x.id === req.workflow.id)
        if (w) publishNow(w, req.spec, req.by, req.note, by)
      }
      requests.decide(r.request, b.route === "approve" ? "approved" : "rejected", by, b.comment ?? null)
      return HttpResponse.json(item(recOf(r.id) ?? r))
    }
    const live_ = live.get(r.id)!
    live_.decision = { route: b.route, label: VERB[b.route] ?? route.label, by, at: new Date().toISOString(), comment: b.comment ?? null, selected: b.selected ?? null }
    const run_ = r.run && findRun(r.run.id)
    if (run_ && r.step && r.kind === "approval") decideWait(run_, r.step, b.route, by)
    return HttpResponse.json(item(live_))
  }),
  http.get("/api/workflows/:id/versions/:n", async ({ params }) => {
    await wait()
    const w = find(params.id)
    const n = Number(params.n)
    const spec = w && specAt(w, n), version = w && versionOf(w, n)
    return spec && version ? HttpResponse.json({ version, spec } satisfies VersionSpec) : notFound()
  }),
  http.post("/api/workflows/:id/versions/:n/restore", async ({ params }) => {
    await wait(300)
    const w = find(params.id)
    const spec = w && specAt(w, Number(params.n))
    if (!w || !spec) return notFound()
    w.draft = mkDraft(w, spec, draftOf(w).revision + 1, who(), 0)
    return HttpResponse.json(w.draft satisfies Draft)
  }),
]

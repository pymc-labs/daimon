// Workflows: live specs, one draft each, versions and runs. Validation runs the same spec logic as the canvas.
import { http, HttpResponse, type HttpHandler } from "msw"

import { clone, diff, insertNode, validate } from "@/modules/workflows/spec"
import type {
  AskReply, Catalog, Draft, DraftPut, NewWorkflow, Person, RunDetail, RunStatus, RunSummary, Spec, Status, Template, Tick, Version, Workflow,
  WorkflowPatch, WorkflowSummary,
} from "@/modules/workflows/types"
import { fromYaml } from "@/modules/workflows/yaml"
import { notFound, unauthorized, wait } from "./util"
import { me } from "./world"
import { SPECS, TEMPLATE_SPECS } from "./workflows-specs"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const P: Record<string, Person> = {
  maya: { id: "p_maya", name: "Maya Lindqvist" }, jonas: { id: "p_jonas", name: "Jonas Weber" },
  priya: { id: "p_priya", name: "Priya Raman" }, alex: { id: "p_alex", name: "Alex Morgan" },
}

export const CATALOG: Catalog = {
  agents: [
    { key: "daimon", id: "ag_daimon", name: "Daimon", tools: 12 }, { key: "mmm", id: "ag_mmm", name: "MMM Analyst", tools: 8 },
    { key: "pricing", id: "ag_pricing", name: "Pricing Analyst", tools: 6 }, { key: "reviewer", id: "ag_review", name: "Code Reviewer", tools: 9 },
    { key: "sales", id: "ag_sales", name: "Sales Desk", tools: 14 },
  ],
  tools: [
    ["gmail/send_message", "Send email", true], ["hubspot/search_crm", "Search CRM", false], ["hubspot/get_object", "Get record", false],
    ["hubspot/create_note", "Create note", true], ["hubspot/tag_contact", "Tag contact", true], ["linear/create_issue", "Create issue", true],
    ["linear/query_issues", "Query issues", false], ["linear/create_project", "Create project", true], ["notion/create_page", "Create page", true],
    ["gdrive/create_folder", "Create folder", true], ["github/create_comment", "Comment on issue", true], ["slack/post_message", "Post message", true],
    ["toggl/project_profitability", "Project profitability", false], ["bluedot/get_transcript", "Meeting transcript", false],
  ].map(([id, label, writes]) => ({ id: id as string, label: label as string, writes: writes as boolean })),
  people: Object.values(P).slice(0, 3),
  channels: [
    ["C0SALES", "sales"], ["C0DELIVERY", "delivery"], ["C0LEADS", "leads"], ["C0TRIAGE", "triage"], ["C0ONCALL", "oncall"],
    ["C0CLIENTS", "clients"], ["C0MMM", "mmm-larkspur"], ["C0PRICING", "pricing"], ["C0ENG", "eng-review"],
  ].map(([id, name]) => ({ id: id!, name: name! })),
}

const TEMPLATES: Template[] = [
  { id: "daily_digest", name: "Daily digest", trigger: "schedule", kinds: ["agent", "branch", "finish"] },
  { id: "webhook_triage", name: "Webhook triage", trigger: "webhook", kinds: ["agent", "finish"] },
  { id: "approve_send", name: "Approve then send", trigger: "manual", kinds: ["agent", "approval", "action"] },
  { id: "research_each", name: "Research each", trigger: "manual", kinds: ["for_each", "agent", "finish"] },
  { id: "review_loop", name: "Review loop", trigger: "manual", kinds: ["agent", "agent", "finish"] },
  { id: "onboarding", name: "Onboarding", trigger: "manual", kinds: ["transform", "action", "wait"] },
]

type Stored = {
  id: string; status: Status; owner: Person; live: Spec | null; version: number | null; draft: Draft | null
  ticks: string; spend: number; failing?: boolean; next_h: number | null; editors: Person[]; runners: Person[]
  source_routine?: { id: string; name: string }; labels?: string[]; runs?: RunSummary[]; cap?: number
}

/** Ticks from a pattern, oldest first: s succeeded, f failed, w waiting, r running, c cancelled. */
const CODE: Record<string, RunStatus> = { s: "succeeded", f: "failed", w: "waiting", r: "running", c: "cancelled", t: "timed_out" }

const W: Stored[] = [
  { id: "wf_lead", status: "live", owner: P.maya!, live: SPECS.lead, version: 7, draft: null, ticks: "ssssfsssws", spend: 18.4, next_h: null,
    editors: [P.jonas!], runners: [P.priya!], labels: ["ana@brightwave.example", "lee@tidewater.example", "sam@larkspur.example", "info@cobalt.example", "jo@northwind.example"] },
  { id: "wf_digest", status: "live", owner: P.jonas!, live: SPECS.digest, version: 3, draft: null, ticks: "ssssssssss", spend: 2.15, next_h: 62, editors: [P.maya!], runners: [] },
  { id: "wf_meeting", status: "live", owner: P.maya!, live: SPECS.meeting, version: 5, draft: null, ticks: "sssssfssss", spend: 9.8, next_h: null, editors: [], runners: [P.jonas!],
    labels: ["Brightwave weekly", "Larkspur Q4 plan", "Tidewater kickoff"] },
  { id: "wf_profit", status: "live", owner: P.jonas!, live: SPECS.profit, version: 2, draft: null, ticks: "ssssss", spend: 1.2, next_h: 450, editors: [P.maya!], runners: [] },
  { id: "wf_triage", status: "live", owner: P.priya!, live: SPECS.triage, version: 11, draft: null, ticks: "ssssssscss", spend: 24.65, next_h: null, editors: [P.jonas!], runners: [],
    labels: ["Adstock decay ignores lag", "Docs: install on Windows", "Support Python 3.14", "Prior predictive fails"] },
  { id: "wf_research", status: "needs_review", owner: P.maya!, live: SPECS.research, version: 2, draft: null, ticks: "sss", spend: 6.3, next_h: null, editors: [], runners: [] },
  { id: "wf_content", status: "live", owner: P.priya!, live: SPECS.content, version: 4, draft: null, ticks: "ssfsssw", spend: 7.9, next_h: null, editors: [P.maya!], runners: [P.maya!],
    labels: ["Priors for media mix", "Why Bayesian pricing", "Adstock in practice"] },
  { id: "wf_onboard", status: "live", owner: P.jonas!, live: SPECS.onboard, version: 6, draft: null, ticks: "ssssw", spend: 4.4, next_h: null, editors: [P.maya!], runners: [P.priya!],
    labels: ["Brightwave Telecom", "Tidewater Foods", "Larkspur Retail"] },
  { id: "wf_alert", status: "live", owner: P.jonas!, live: SPECS.alert, version: 9, draft: null, ticks: "ssfsffsftf", spend: 12.75, failing: true, next_h: null, editors: [P.maya!], runners: [],
    labels: ["api-latency-p99", "worker-queue-depth", "db-cpu"] },
  { id: "wf_readout", status: "live", owner: P.maya!, live: SPECS.readout, version: 1, draft: null, ticks: "ssss", spend: 0.85, next_h: 60, editors: [], runners: [],
    source_routine: { id: "r1", name: "Weekly channel readout" } },
  { id: "wf_price", status: "live", owner: P.priya!, live: SPECS.price, version: 1, draft: null, ticks: "sssssff", spend: 1.6, failing: true, next_h: 14, editors: [], runners: [],
    source_routine: { id: "r2", name: "Overnight price test" } },
  { id: "wf_prs", status: "paused", owner: P.priya!, live: SPECS.prs, version: 1, draft: null, ticks: "sss", spend: 0.3, next_h: null, editors: [], runners: [],
    source_routine: { id: "r3", name: "Stale pull requests" } },
  { id: "wf_board", status: "draft", owner: P.maya!, live: null, version: null, draft: null, ticks: "", spend: 0, next_h: null, editors: [], runners: [] },
  { id: "wf_renewal", status: "needs_owner", owner: P.alex!, live: SPECS.renewal, version: 3, draft: null, ticks: "ssssssssss", spend: 3.1, next_h: 20, editors: [], runners: [] },
]

const mkDraft = (w: Stored, spec: Spec, revision: number, by: Person = w.owner, h = 2): Draft => ({
  revision, base_version: w.version, spec, changes: diff(w.live, spec), validation: validate(spec), updated_at: ago(h), updated_by: by,
})

// Inbound lead: research instructions changed, a deal note added after send (a typo in its note reference), spam tagging with no next step.
{
  const lead = W[0]!, s = clone(SPECS.lead)
  s.nodes.research!.instructions = "Find the sender's company, its size, industry and what it likely needs.\nUse web search and HubSpot reads. Contact nobody.\n"
  s.nodes.research!.routes!.unqualified = { to: "tag_spam", when: "Spam, students, job seekers or no fit." }
  s.nodes.send!.next = "log_deal"
  s.nodes.log_deal = { kind: "action", title: "Log the deal", tool: "hubspot/create_note", args: { email: "${{ inputs.email }}", note: "${{ steps.research.output.note }}" }, next: "done" }
  s.nodes.tag_spam = { kind: "action", title: "Tag as spam", tool: "hubspot/tag_contact", args: { email: "${{ inputs.email }}", tag: "spam" } }
  lead.draft = mkDraft(lead, s, 4, P.maya!, 0.5)
  const board = W.find((w) => w.id === "wf_board")!
  board.draft = mkDraft(board, SPECS.board, 2, P.maya!, 30)
}

const NOTES = ["Tighter research instructions", "Approval times out after two days", "Reply drafts propose a call", "Adds HubSpot reads", "First version"]
const versionsOf = (w: Stored): Version[] => !w.version ? [] : Array.from({ length: w.version }, (_, i) => {
  const n = w.version! - i
  const by = i % 3 === 1 && w.editors[0] ? w.editors[0] : w.owner
  return { number: n, published_by: by, published_at: ago(24 * (i * 6 + 3)), note: n === 1 ? "First version" : NOTES[i % (NOTES.length - 1)]!,
    grants: n === w.version && w.id === "wf_triage" ? ["builtins", "untrusted_waiver"] : [], runs: i === 0 ? 40 + w.ticks.length * 3 : 12 - Math.min(i, 8), live: i === 0 }
})

const DUR: Record<string, number> = { s: 95_000, f: 41_000, c: 12_000, t: 3_600_000 }
function runsOf(w: Stored): RunSummary[] {
  if (w.runs) return w.runs
  const trig = w.live?.triggers?.[0] ?? { id: "manual", kind: "manual" as const }
  const nodes = Object.keys(w.live?.nodes ?? {})
  const n = w.ticks.length
  w.runs = [...w.ticks].reverse().map((c, i) => ({
    id: `${w.id}_run${n - i}`, number: 120 + n - i, status: CODE[c]!, label: w.labels ? w.labels[i % w.labels.length]! : null, trigger: { kind: trig.kind, id: trig.id },
    started_at: ago(i * 19 + 1.5), duration_ms: c === "w" || c === "r" ? null : DUR[c]! + ((i * 7919) % 60_000), cost_usd: Math.round((0.12 + ((i * 37) % 50) / 100) * 100) / 100,
    current_step: c === "w" ? nodes.find((k) => w.live!.nodes[k]!.kind === "approval" || w.live!.nodes[k]!.kind === "wait") ?? null : c === "f" ? nodes[1] ?? null : null,
    version: w.version ?? 0, test: false,
  }))
  return w.runs
}
const ticksOf = (w: Stored): Tick[] => runsOf(w).slice(0, 10).reverse().map((r) => ({ number: r.number, status: r.status, at: r.started_at }))

const specOf = (w: Stored) => w.live ?? w.draft!.spec
function summary(w: Stored): WorkflowSummary {
  const m = me()!
  const spec = specOf(w)
  const vis = spec.visibility ?? "private"
  return {
    id: w.id, name: spec.name, goal: spec.goal ?? "", status: w.status, owner: w.owner, run_as: w.status === "needs_owner" ? null : w.owner,
    live_version: w.version, draft_revision: w.draft?.revision ?? null, draft_changes: w.draft?.changes.length ?? 0,
    triggers: spec.triggers ?? [], ticks: ticksOf(w), next_run: w.status === "live" && w.next_h ? ago(-w.next_h) : null, spend_usd: w.spend,
    visibility: vis, mine: w.owner.id === m.id, shared_with_me: vis === "shared" && w.owner.id !== m.id,
    failing: !!w.failing, steps: Object.keys(spec.nodes).length, source_routine: w.source_routine ?? null,
  }
}
function detail(w: Stored): Workflow {
  const m = me()!
  const spec = specOf(w)
  const admin = m.role === "owner" || m.role === "admin"
  return {
    ...summary(w), spec, home_name: CATALOG.channels.find((c) => c.id === spec.home?.channel)?.name ?? "",
    editors: w.editors, runners: w.runners, viewers: spec.visibility === "shared" ? CATALOG.people.filter((p) => p.id !== w.owner.id && !w.editors.includes(p)) : [],
    monthly_cap_usd: w.cap ?? 50, can_edit: admin || w.owner.id === m.id || w.editors.some((e) => e.id === m.id),
    can_run: admin || w.owner.id === m.id || [...w.editors, ...w.runners].some((e) => e.id === m.id), updated_at: w.draft?.updated_at ?? ago(30),
  }
}
const visible = () => {
  const m = me()
  if (!m) return []
  return W.filter((w) => m.role === "owner" || m.role === "admin" || w.owner.id === m.id || specOf(w).visibility === "shared")
}
const find = (id: unknown) => visible().find((w) => w.id === id)
const draftOf = (w: Stored): Draft => w.draft ?? mkDraft(w, clone(w.live!), 0)

// ---------- Ask Daimon: one canned edit per prompt ----------

function ask(spec: Spec, text: string): { reply: string; spec: Spec; changed: string[] } {
  const s = clone(spec)
  const t = text.toLowerCase()
  const ids = Object.keys(s.nodes)
  const finish = ids.find((k) => s.nodes[k]!.kind === "finish" && s.nodes[k]!.outcome !== "failed")
  if (t.includes("approv")) {
    const target = ids.find((k) => s.nodes[k]!.kind === "action") ?? finish
    const from = ids.find((k) => [s.nodes[k]!.next].flat().includes(target))
    if (!target || !from) return { reply: "There is no step to put an approval in front of.", spec, changed: [] }
    const r = insertNode(s, { from, slot: "next.0", to: target }, "approval")
    const n = r.spec.nodes[r.id]!
    n.title = "Approve before sending"
    n.binds = target
    n.routes = { approve: target, reject: finish ?? target }
    return { reply: `Added an approval before ${r.spec.nodes[target]!.title ?? target}. It binds that step, so the approver sees exactly what will run.`, spec: r.spec, changed: [r.id] }
  }
  if (t.includes("post") || t.includes("#")) {
    if (!finish) return { reply: "There is no Finish step to deliver from.", spec, changed: [] }
    s.nodes[finish]!.deliver = { to: "#sales", message: s.nodes[finish]!.deliver?.message ?? "${{ steps." + ids[0] + ".summary }}" }
    return { reply: "The result now posts to #sales when the run finishes.", spec: s, changed: [finish] }
  }
  if (t.includes("retry") || t.includes("again")) {
    const id = ids.find((k) => s.nodes[k]!.kind === "action") ?? ids[0]!
    s.nodes[id]!.retry = { attempts: 2 }
    return { reply: `${s.nodes[id]!.title ?? id} now retries twice before it fails.`, spec: s, changed: [id] }
  }
  if (t.includes("cap") || t.includes("$") || t.includes("budget")) {
    s.defaults = { ...s.defaults, budget_usd: 1 }
    const agents = ids.filter((k) => s.nodes[k]!.kind === "agent")
    return { reply: "Each agent step now stops at $1.", spec: s, changed: agents }
  }
  return { reply: "Try one of the suggestions below. Each one makes a single edit you can undo.", spec, changed: [] }
}

const DESCRIBED = (text: string): Spec => ({
  schema: "daimon.workflow/v1", name: "Risky deals digest", goal: text.slice(0, 120), visibility: "private", home: { channel: "C0SALES" },
  triggers: [{ id: "monday", kind: "schedule", cron: "0 9 * * 1", timezone: "Europe/Stockholm", catch_up: "run-once" }],
  defaults: { agent: "sales", budget_usd: 1 }, start: "collect",
  nodes: {
    collect: { kind: "agent", title: "Find risky deals", instructions: "List open deals with no activity in 14 days or a slipped close date.",
      tools: ["hubspot/search_crm"], output: { deals: { type: "array", items: { type: "object" } }, digest: { type: "string" } }, next: "review" },
    review: { kind: "approval", title: "Check the digest", approvers: ["owner"], show: "${{ steps.collect.output.digest }}", routes: { approve: "done", reject: "skipped" } },
    done: { kind: "finish", title: "Post to sales", deliver: { to: "home", message: "${{ steps.collect.output.digest }}" } },
    skipped: { kind: "finish", title: "Skipped", outcome: "failed" },
  },
})

let seq = 100
function create(body: NewWorkflow): { w: Stored; reply?: string } | { error: string } {
  const m = me()!
  let spec: Spec
  let reply: string | undefined
  if (body.from === "describe") {
    spec = DESCRIBED(body.text)
    reply = "Drafted Risky deals digest: every Monday at 09:00 the Sales Desk finds deals with no activity in 14 days, you check the digest, then it posts to #sales."
  } else if (body.from === "template") {
    const t = TEMPLATE_SPECS[body.template]
    if (!t) return { error: "Unknown template" }
    spec = clone(t)
  } else if (body.from === "blank") {
    spec = { schema: "daimon.workflow/v1", name: body.name || "Untitled workflow", visibility: "private", triggers: [{ id: "manual", kind: "manual" }],
      start: "agent", nodes: { agent: { kind: "agent", title: "New agent step", instructions: "", next: "done" }, done: { kind: "finish", title: "Finish" } } }
  } else {
    const r = fromYaml(body.yaml)
    if (r.error != null) return { error: `Line ${r.line}: ${r.error}` }
    spec = r.spec
  }
  const me_ = { id: m.id, name: m.name }
  const w: Stored = { id: `wf_${++seq}`, status: "draft", owner: me_, live: null, version: null, draft: null, ticks: "", spend: 0, next_h: null, editors: [], runners: [] }
  w.draft = mkDraft(w, spec, 1, me_, 0)
  W.unshift(w)
  return { w, reply }
}

export const workflowsHandlers: HttpHandler[] = [
  http.get("/api/workflows", async () => {
    await wait()
    return me() ? HttpResponse.json(visible().map(summary)) : unauthorized()
  }),
  http.get("/api/workflows/templates", async () => {
    await wait(60)
    return me() ? HttpResponse.json(TEMPLATES) : unauthorized()
  }),
  http.get("/api/workflows/catalog", async () => {
    await wait(60)
    return me() ? HttpResponse.json(CATALOG) : unauthorized()
  }),
  http.post("/api/workflows", async ({ request }) => {
    await wait(500)
    if (!me()) return unauthorized()
    const r = create((await request.json()) as NewWorkflow)
    if ("error" in r) return HttpResponse.json({ detail: r.error }, { status: 422 })
    return HttpResponse.json({ ...detail(r.w), reply: r.reply ?? null }, { status: 201 })
  }),
  http.get("/api/workflows/:id", async ({ params }) => {
    await wait()
    if (!me()) return unauthorized()
    const w = find(params.id)
    return w ? HttpResponse.json(detail(w)) : notFound()
  }),
  http.patch("/api/workflows/:id", async ({ params, request }) => {
    await wait()
    const w = find(params.id)
    if (!w) return notFound()
    const b = (await request.json()) as WorkflowPatch
    if (b.paused != null && w.live) w.status = b.paused ? "paused" : "live"
    if (b.name) (w.live ?? w.draft!.spec).name = b.name
    return HttpResponse.json(detail(w))
  }),
  http.delete("/api/workflows/:id", async ({ params }) => {
    await wait()
    const i = W.findIndex((w) => w.id === params.id)
    if (i < 0) return notFound()
    W.splice(i, 1)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get("/api/workflows/:id/draft", async ({ params }) => {
    await wait()
    const w = find(params.id)
    return w ? HttpResponse.json(draftOf(w)) : notFound()
  }),
  http.put("/api/workflows/:id/draft", async ({ params, request }) => {
    await wait(120)
    const w = find(params.id)
    if (!w) return notFound()
    const b = (await request.json()) as DraftPut
    const cur = draftOf(w)
    if (b.base_revision !== cur.revision) return HttpResponse.json({ detail: "Someone else changed this draft.", draft: cur }, { status: 409 })
    w.draft = mkDraft(w, b.spec, cur.revision + 1, { id: me()!.id, name: me()!.name }, 0)
    return HttpResponse.json(w.draft)
  }),
  http.delete("/api/workflows/:id/draft", async ({ params }) => {
    await wait()
    const w = find(params.id)
    if (!w) return notFound()
    if (w.live) w.draft = null
    return HttpResponse.json(draftOf(w))
  }),
  http.post("/api/workflows/:id/draft/ask", async ({ params, request }) => {
    await wait(900)
    const w = find(params.id)
    if (!w) return notFound()
    const { text } = (await request.json()) as { text: string }
    const cur = draftOf(w)
    const r = ask(cur.spec, text)
    if (r.changed.length) w.draft = mkDraft(w, r.spec, cur.revision + 1, { id: me()!.id, name: me()!.name }, 0)
    const body: AskReply = { reply: r.reply, draft: draftOf(w), changed: r.changed }
    return HttpResponse.json(body)
  }),
  http.post("/api/workflows/:id/draft/publish", async ({ params }) => {
    await wait(600)
    const w = find(params.id)
    if (!w?.draft) return notFound()
    if (w.draft.validation.errors.length) return HttpResponse.json({ detail: "Fix the problems first." }, { status: 422 })
    w.live = w.draft.spec
    w.version = (w.version ?? 0) + 1
    w.draft = null
    if (w.status === "draft") w.status = "live"
    return HttpResponse.json(detail(w))
  }),
  http.post("/api/workflows/:id/validate", async ({ params, request }) => {
    await wait(60)
    if (!find(params.id)) return notFound()
    const { spec } = (await request.json()) as { spec: Spec }
    return HttpResponse.json(validate(spec))
  }),
  http.get("/api/workflows/:id/versions", async ({ params }) => {
    await wait()
    const w = find(params.id)
    return w ? HttpResponse.json(versionsOf(w)) : notFound()
  }),
  http.get("/api/workflows/:id/runs", async ({ params }) => {
    await wait()
    const w = find(params.id)
    return w ? HttpResponse.json(runsOf(w)) : notFound()
  }),
  http.post("/api/workflows/:id/runs", async ({ params, request }) => {
    await wait(300)
    const w = find(params.id)
    if (!w) return notFound()
    const b = (await request.json()) as { inputs: Record<string, unknown>; test?: boolean }
    const runs = runsOf(w)
    const spec = specOf(w)
    const r: RunSummary = {
      id: `${w.id}_run${Date.now()}`, number: (runs[0]?.number ?? 0) + 1, status: "running", label: spec.label ? String(Object.values(b.inputs)[0] ?? "") || null : null,
      trigger: { kind: "manual", id: "manual" }, started_at: new Date().toISOString(), duration_ms: null, current_step: spec.start, cost_usd: 0,
      version: w.version ?? 0, test: !!b.test,
    }
    runs.unshift(r)
    return HttpResponse.json(r, { status: 201 })
  }),
  // Milestone B: the run page. A summary with inputs and no step detail yet.
  http.get("/api/workflows/:id/runs/:run", async ({ params }) => {
    await wait()
    const w = find(params.id)
    const r = w && runsOf(w).find((x) => x.id === params.run)
    if (!r) return notFound()
    const body: RunDetail = { ...r, inputs: {}, steps: [], triggered_by: null }
    return HttpResponse.json(body)
  }),
  http.get("/api/workflows/:id/runs/:run/events", async () => {
    await wait()
    return HttpResponse.json([])
  }),
]

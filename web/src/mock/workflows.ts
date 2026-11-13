// Workflows: live specs, one draft each, versions and runs. Validation runs the same spec logic as the canvas.
import { http, HttpResponse, type HttpHandler } from "msw"

import { allEdges, clone, diff, insertNode, titleOf, toolWrites, validate } from "@/modules/workflows/spec"
import { ACTIVE_RUN, project } from "@/modules/workflows/runs"
import type {
  AskReply, AttentionItem, Catalog, Draft, DraftPut, NewWorkflow, Person, RunTest, Spec, Status, Template, Tick, Version, Workflow,
  WorkflowPatch, WorkflowSummary,
} from "@/modules/workflows/types"
import { fromYaml } from "@/modules/workflows/yaml"
import { notFound, sleep, unauthorized, wait } from "./util"
import { me } from "./world"
import {
  cancel, chain, decideUnknown, decideWait, detailOf, redrive, start, stepDetail, summaryOf, verify, visible as released, type Decision, type Run, type Script,
} from "./workflows-runs"
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
  source_routine?: { id: string; name: string }; labels?: string[]; cap?: number
}

const W: Stored[] = [
  { id: "wf_lead", status: "live", owner: P.maya!, live: SPECS.lead, version: 7, draft: null, ticks: "ssfssTsswr", spend: 18.4, next_h: null,
    editors: [P.jonas!], runners: [P.priya!], labels: ["ana@brightwave.example", "lee@tidewater.example", "sam@larkspur.example", "info@cobalt.example", "jo@northwind.example"] },
  { id: "wf_digest", status: "live", owner: P.jonas!, live: SPECS.digest, version: 3, draft: null, ticks: "ssssssssss", spend: 2.15, next_h: 62, editors: [P.maya!], runners: [] },
  { id: "wf_meeting", status: "live", owner: P.maya!, live: SPECS.meeting, version: 5, draft: null, ticks: "sssssfssss", spend: 9.8, next_h: null, editors: [], runners: [P.jonas!],
    labels: ["Brightwave weekly", "Larkspur Q4 plan", "Tidewater kickoff"] },
  { id: "wf_profit", status: "live", owner: P.jonas!, live: SPECS.profit, version: 2, draft: null, ticks: "ssssss", spend: 1.2, next_h: 450, editors: [P.maya!], runners: [] },
  { id: "wf_triage", status: "live", owner: P.priya!, live: SPECS.triage, version: 11, draft: null, ticks: "ssssssscss", spend: 24.65, next_h: null, editors: [P.jonas!], runners: [],
    labels: ["Adstock decay ignores lag", "Docs: install on Windows", "Support Python 3.14", "Prior predictive fails"] },
  { id: "wf_research", status: "needs_review", owner: P.maya!, live: SPECS.research, version: 2, draft: null, ticks: "sssr", spend: 6.3, next_h: null, editors: [], runners: [] },
  { id: "wf_content", status: "live", owner: P.priya!, live: SPECS.content, version: 4, draft: null, ticks: "ssfsssw", spend: 7.9, next_h: null, editors: [P.maya!], runners: [P.maya!],
    labels: ["Priors for media mix", "Why Bayesian pricing", "Adstock in practice"] },
  { id: "wf_onboard", status: "live", owner: P.jonas!, live: SPECS.onboard, version: 6, draft: null, ticks: "ssssu", spend: 4.4, next_h: null, editors: [P.maya!], runners: [P.priya!],
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

// ---------- runs: ticks are a pattern, oldest first (s succeeded, f failed, w waiting, r running, c cancelled, t timed out, u unknown, T test) ----------

const T0 = Date.now()
const RUNS = new Map<string, Run[]>()
const COMPETITORS = ["Northwind", "Cobalt", "Tidewater", "Larkspur", "Brightwave", "Fernhill", "Juniper", "Halcyon", "Meridian", "Quarry", "Sable", "Wren"]
const INPUT: Record<string, unknown> = {
  name: "Ana Souza", message: "Hi, we run 40 stores and want to forecast promotions before Q1. Could we talk next week?", contact_email: "ops@brightwave.example",
  kickoff_date: "2026-11-20", competitors: COMPETITORS, audience: "Marketing leads", repo: "pymc-labs/pymc-marketing",
}
function inputsFor(spec: Spec, label: string | null): Record<string, unknown> {
  const labelled = spec.label?.match(/inputs\.(\w+)/)?.[1]
  return Object.fromEntries(Object.entries(spec.inputs?.properties ?? {}).map(([k, f]) => [k,
    k === labelled && label ? label : k in INPUT ? INPUT[k] : f.type === "array" ? ["First", "Second"] : f.format === "email" ? "ana@brightwave.example" : `Sample ${k}`]))
}
/** Nodes in the order a default run meets them. */
function order(spec: Spec) {
  const out: string[] = [], seen = new Set<string>(), q = [spec.start]
  while (q.length) {
    const id = q.shift()!
    if (seen.has(id) || !spec.nodes[id]) continue
    seen.add(id)
    out.push(id)
    q.push(...allEdges(spec).filter((e) => e.from === id).map((e) => e.to))
  }
  return out
}
function scriptFor(w: Stored, spec: Spec, code: string, i: number): Script {
  const ids = order(spec)
  const first = (...kinds: string[]) => ids.find((id) => kinds.includes(spec.nodes[id]!.kind)) ?? spec.start
  const writes = ids.find((id) => spec.nodes[id]!.kind === "action" && toolWrites(spec.nodes[id]!.tool)) ?? first("action", "agent")
  const choose: Script["choose"] = w.id === "wf_content" ? { critique: ["routes.revise", "routes.accept"] }
    : w.id === "wf_digest" && i % 3 === 2 ? { risky: ["else"] } : w.id === "wf_lead" && i === 4 ? { research: ["routes.unqualified"] } : undefined
  const end: Script["end"] = code === "f" ? { node: w.id === "wf_lead" ? "draft_reply" : first("agent", "code", "action"), as: "fail" }
    : code === "w" ? { node: first("approval", "wait"), as: "wait", visit: 1 }
    : code === "c" ? { node: first("agent", "action"), as: "cancel" }
    : code === "t" ? { node: first("wait", "agent"), as: "timeout" }
    : code === "u" ? { node: writes, as: "unknown" } : undefined
  return { choose, end }
}
function runsOf(w: Stored): Run[] {
  const have = RUNS.get(w.id)
  if (have) return have
  const spec = w.live
  const n = w.ticks.length
  const trig = spec?.triggers?.[0] ?? { id: "manual", kind: "manual" as const }
  const runs = !spec ? [] : [...w.ticks].reverse().map((code, i) => {
    const number = 119 + n - i
    const label = spec.label && w.labels ? w.labels[i % w.labels.length]! : null
    const run: Run = {
      id: `${w.id}_run${number}`, wf: { id: w.id, name: spec.name }, number, spec, version: code === "T" ? w.version! : w.version! - (i > 6 ? 1 : 0), inputs: inputsFor(spec, label),
      label, trigger: code === "T" ? { kind: "manual", id: "manual" } : { kind: trig.kind, id: trig.id },
      delivery: trig.kind === "webhook" && code !== "T" ? `dlv_${chain("", w.id + number).slice(0, 10)}` : null, by: code === "T" ? P.maya! : null, owner: w.owner,
      test: code === "T" ? { live_writes: false, pinned: [] } : null, parent: null, script: scriptFor(w, spec, code, i), events: [],
    }
    const at = code === "r" ? T0 - (w.id === "wf_research" ? 75_000 : 25_000) : T0 - (i * 19 + 1.5) * 3_600_000
    start(run, at)
    return run
  })
  RUNS.set(w.id, runs)
  return runs
}
const ticksOf = (w: Stored): Tick[] => runsOf(w).filter((r) => !r.test).slice(0, 10).reverse().map((r) => {
  const s = summaryOf(r)
  return { number: r.number, status: s.status, at: s.started_at }
})
const allRuns = () => visible().flatMap((w) => runsOf(w))
const findRun = (id: unknown) => allRuns().find((r) => r.id === id)

/** Releases ledger rows as their time comes; resumes after `Last-Event-ID`. */
function runStream(run: Run, after: number) {
  const enc = new TextEncoder()
  let closed = false
  const body = new ReadableStream({
    async start(ctrl) {
      let seq = after
      while (!closed) {
        for (const e of released(run).filter((x) => x.seq > seq)) {
          ctrl.enqueue(enc.encode(`id: ${e.seq}\ndata: ${JSON.stringify(e)}\n\n`))
          seq = e.seq
        }
        if (!ACTIVE_RUN.includes(project(released(run)).status) && seq >= run.events.length) break
        await sleep(400)
      }
      if (!closed) ctrl.close()
    },
    cancel() { closed = true },
  })
  return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store" } })
}

function attention(): AttentionItem[] {
  const m = me()!
  const out: AttentionItem[] = []
  for (const w of visible()) {
    const name = specOf(w).name
    if (w.status === "needs_owner" || w.status === "needs_review")
      out.push({ kind: w.status, workflow: { id: w.id, name }, run: null, step: null, since: ago(30), expires_at: null, detail: w.status === "needs_owner" ? `${w.owner.name} left` : "Changed grants" })
    for (const r of runsOf(w)) {
      const d = detailOf(r)
      const run = { id: r.id, number: r.number }
      if (d.status === "failed" && !d.redriven && r === runsOf(w).find((x) => !x.test && summaryOf(x).status !== "running"))
        out.push({ kind: "failed", workflow: { id: w.id, name }, run, step: d.failure?.step ?? null, since: d.ended_at!, expires_at: null, detail: d.failure?.message ?? "" })
      const open = d.steps.find((s) => s.status === "unknown" || (s.wait?.status === "waiting" && s.wait.kind === "approval" && s.wait.addressees.some((p) => p.id === m.id)))
      if (!open || d.status !== "waiting") continue
      out.push({
        kind: open.status === "unknown" ? "unknown" : "approval", workflow: { id: w.id, name }, run, step: open.key, since: open.wait?.opened_at ?? open.started_at!,
        expires_at: open.wait?.expires_at ?? null, detail: open.status === "unknown" ? open.reason ?? "" : titleOf(open.node, r.spec.nodes[open.node]),
      })
    }
  }
  return out.sort((a, b) => b.since.localeCompare(a.since))
}

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
    return w ? HttpResponse.json(runsOf(w).map((r) => summaryOf(r))) : notFound()
  }),
  http.post("/api/workflows/:id/runs", async ({ params, request }) => {
    await wait(300)
    const w = find(params.id)
    if (!w) return notFound()
    const b = (await request.json()) as { inputs: Record<string, unknown>; test?: RunTest | null }
    const runs = runsOf(w)
    const spec = b.test ? draftOf(w).spec : specOf(w)
    const m = me()!
    const label = spec.label?.match(/inputs\.(\w+)/)?.[1]
    const run: Run = {
      id: `${w.id}_run${(runs[0]?.number ?? 119) + 1}`, wf: { id: w.id, name: spec.name }, number: (runs[0]?.number ?? 119) + 1, spec, version: w.version ?? 0,
      inputs: b.inputs, label: label ? String(b.inputs[label] ?? "") || null : null, trigger: { kind: "manual", id: "manual" }, delivery: null,
      by: { id: m.id, name: m.name }, owner: w.owner, test: b.test ?? null, parent: null, script: { pace: b.test ? 0.15 : 0.4, decide_after: 2 }, events: [],
    }
    start(run, Date.now())
    runs.unshift(run)
    return HttpResponse.json(summaryOf(run), { status: 201 })
  }),
  http.get("/api/workflows/:id/deliveries/last", async ({ params }) => {
    await wait(250)
    const w = find(params.id)
    const r = w && runsOf(w).find((x) => x.delivery)
    return r ? HttpResponse.json({ id: r.delivery, received_at: r.events[0]!.at, inputs: r.inputs }) : notFound()
  }),
  http.get("/api/workflows/:id/runs/:n", async ({ params }) => {
    await wait()
    const w = find(params.id)
    const r = w && runsOf(w).find((x) => String(x.number) === params.n)
    return r ? HttpResponse.json(detailOf(r)) : notFound()
  }),
]

const runOps: Record<string, (r: Run, body: Record<string, unknown>, by: Person) => Run | boolean | void> = {
  cancel: (r, _, by) => cancel(r, by),
  redrive: (r, _, by) => redrive(r, by),
  decide: (r, b, by) => decideUnknown(r, b as Decision, by),
  approve: (r, b, by) => decideWait(r, String(b.step), String(b.route), by),
  rerun: (r, b, by) => fork(r, by, b),
  fork: (r, b, by) => fork(r, by, b),
}
/** Run again (no step) or Run from here: a new run on the chosen version, sharing the parent's finished upstream steps. */
function fork(r: Run, by: Person, b: Record<string, unknown>) {
  const w = W.find((x) => x.id === r.wf.id)!
  const runs = runsOf(w)
  const spec = b.version === "draft" ? draftOf(w).spec : b.version === "live" ? specOf(w) : r.spec
  const number = runs[0]!.number + 1
  const run: Run = {
    ...r, id: `${w.id}_run${number}`, number, spec, version: b.version === "live" ? w.version ?? r.version : r.version, inputs: (b.inputs as Record<string, unknown>) ?? r.inputs,
    trigger: { kind: "manual", id: "manual" }, delivery: null, by, test: b.version === "draft" || r.test ? r.test ?? { live_writes: false, pinned: [] } : null,
    parent: b.step ? { id: r.id, number: r.number } : null, script: { pace: 0.4, decide_after: 2 }, events: [],
  }
  start(run, Date.now(), b.step ? { parent: r, from: String(b.step) } : undefined)
  runs.unshift(run)
  return run
}

/** Run handlers go before `/api/workflows/:id`, which would match `/api/workflows/runs`. */
export const workflowRunsHandlers: HttpHandler[] = [
  http.get("/api/workflows/runs", async () => {
    await wait()
    if (!me()) return unauthorized()
    const rows = allRuns().map((r) => summaryOf(r)).sort((a, b) => b.started_at.localeCompare(a.started_at))
    return HttpResponse.json(rows)
  }),
  http.get("/api/workflows/attention", async () => {
    await wait()
    return me() ? HttpResponse.json(attention()) : unauthorized()
  }),
  http.get("/api/workflows/runs/:run", async ({ params }) => {
    await wait()
    const r = findRun(params.run)
    return r ? HttpResponse.json(detailOf(r)) : notFound()
  }),
  http.get("/api/workflows/runs/:run/events", async ({ params }) => {
    await wait()
    const r = findRun(params.run)
    return r ? HttpResponse.json(released(r)) : notFound()
  }),
  http.get("/api/workflows/runs/:run/verify", async ({ params }) => {
    await wait(500)
    const r = findRun(params.run)
    return r ? HttpResponse.json({ ok: verify(released(r)), events: released(r).length }) : notFound()
  }),
  http.get("/api/workflows/runs/:run/stream", ({ params, request }) => {
    const r = findRun(params.run)
    return r ? runStream(r, Number(request.headers.get("Last-Event-ID")) || 0) : notFound()
  }),
  http.get("/api/workflows/runs/:run/steps/:key", async ({ params, request }) => {
    await wait(120)
    const r = findRun(params.run)
    const a = Number(new URL(request.url).searchParams.get("attempt")) || null
    const d = r && stepDetail(r, decodeURIComponent(String(params.key)), a)
    return d ? HttpResponse.json(d) : notFound()
  }),
  http.post("/api/workflows/runs/:run/:op", async ({ params, request }) => {
    await wait(300)
    const r = findRun(params.run)
    const op = runOps[String(params.op)]
    if (!r || !op) return notFound()
    const m = me()!
    const out = op(r, (await request.json().catch(() => ({}))) as Record<string, unknown>, { id: m.id, name: m.name })
    if (out === false) return HttpResponse.json({ detail: "This step is no longer waiting." }, { status: 409 })
    return HttpResponse.json(summaryOf(typeof out === "object" ? out : r))
  }),
]

// Scripted runs: a walk over the spec writes each run's ledger. Live runs hold rows in the future that the stream releases on time.
import { project } from "@/modules/workflows/runs"
import { allEdges, ancestors, edgeKey, edgesOf, finishDeliver, outputFields, titleOf, toolWrites, type FlowEdge } from "@/modules/workflows/spec"
import type {
  ActivityLine, EventType, Field, Node, Person, Proposal, RunDetail, RunEvent, RunSummary, RunTest, Spec, StepDetail, TriggerKind, WaitKind,
} from "@/modules/workflows/types"
import { AGENTS } from "./world"
import { CATALOG } from "./workflows"

const S = 1000, M = 60 * S, H = 60 * M
const iso = (t: number) => new Date(t).toISOString()
const ms = (s: string) => new Date(s).getTime()
const round2 = (n: number) => Math.round(n * 100) / 100

export type End = "fail" | "unknown" | "wait" | "cancel" | "timeout"
export type Script = {
  /** Edge slot per node, one per visit: `{ critique: ["routes.revise", "routes.accept"] }`. */
  choose?: Record<string, string[]>
  /** Where the run stops and how; `visit` defaults to the first. */
  end?: { node: string; as: End; visit?: number }
  /** Time scale: 1 is the default pace, below 1 is faster. */
  pace?: number
  /** Minutes until an approval is decided. */
  decide_after?: number
}
export type Run = {
  id: string; wf: { id: string; name: string }; number: number; spec: Spec; version: number; inputs: Record<string, unknown>
  label: string | null; trigger: { kind: TriggerKind; id: string }; delivery: string | null; by: Person | null; owner: Person
  test: RunTest | null; parent: { id: string; number: number } | null; script: Script; events: RunEvent[]
}
type Raw = Omit<RunEvent, "seq" | "hash">
type Queued = { node: string; at: number; via: string[] }
type Ctx = { run: Run; raw: Raw[]; visits: Map<string, number>; arrivals: Map<string, string[]>; queue: Queued[]; stop: number | null }

// ---------- ledger chain ----------

function fnv(s: string, seed: number) {
  let h = (0x811c9dc5 ^ seed) >>> 0
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193)
  return (h >>> 0).toString(16).padStart(8, "0")
}
/** A stand-in for sha256(prev_hash || canonical_json(row)): 64 hex characters, chained. */
export const chain = (prev: string, row: unknown) => Array.from({ length: 8 }, (_, i) => fnv(prev + JSON.stringify(row), i)).join("")
const rand = (seed: string) => parseInt(fnv(seed, 7).slice(0, 6), 16) / 0xffffff
export const GENESIS = "0".repeat(64)

/** Re-hashes every row; false when any row was changed after it was written. */
export const verify = (events: RunEvent[]) =>
  events.every((e, i) => e.hash === chain(events[i - 1]?.hash ?? GENESIS, { ...e, hash: undefined }))

function seal(c: Ctx) {
  const raw = c.stop == null ? c.raw : c.raw.filter((r) => ms(r.at) <= c.stop!)
  raw.sort((a, b) => ms(a.at) - ms(b.at))
  for (const r of raw) {
    const e: RunEvent = { seq: c.run.events.length + 1, ...r, hash: "" }
    e.hash = chain(c.run.events.at(-1)?.hash ?? GENESIS, { ...e, hash: undefined })
    c.run.events.push(e)
  }
}

// ---------- the walk ----------

const DUR: Record<Node["kind"], number> = {
  agent: 42 * S, action: 3 * S, transform: 400, branch: 300, code: 5 * S, finish: 400, approval: 0, wait: 0, for_each: 0, workflow: 50 * S,
}
const agentOf = (spec: Spec, n: Node) => n.agent ?? spec.defaults?.agent ?? "daimon"
export const agentName = (key: string) => CATALOG.agents.find((a) => a.key === key)?.name ?? key
function durMs(s?: string) {
  const m = s?.match(/^(\d+)\s*([smhd])$/)
  return m ? Number(m[1]) * { s: S, m: M, h: H, d: 24 * H }[m[2] as "s"] : null
}
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

function emit(c: Ctx, at: number, type: EventType, step: string | null, actor: string, detail: string, data: Record<string, unknown> = {}) {
  c.raw.push({ at: iso(at), type, step, actor, detail, data })
}

function ctx(run: Run): Ctx {
  const visits = new Map<string, number>()
  for (const e of run.events) {
    if (e.type !== "step.ready" || e.data.item) continue
    const node = String(e.data.node)
    visits.set(node, Math.max(visits.get(node) ?? 0, Number(e.data.visit ?? 1)))
  }
  // Branches that already reached a join not yet run, so a resumed run can complete it.
  const done = new Set(run.events.filter((e) => e.type === "step.succeeded" && e.step).map((e) => e.step!.replace(/[#[].*$/, "")))
  const arrivals = new Map<string, string[]>()
  for (const [id, n] of Object.entries(run.spec.nodes)) {
    if (n.join !== "all" || visits.has(id)) continue
    const got = allEdges(run.spec).filter((e) => e.to === id && done.has(e.from)).map(edgeKey)
    if (got.length) arrivals.set(id, got)
  }
  return { run, raw: [], visits, arrivals, queue: [], stop: null }
}

function drive(c: Ctx) {
  while (c.queue.length) {
    c.queue.sort((a, b) => a.at - b.at)
    const q = c.queue.shift()!
    if (c.stop != null && q.at > c.stop) break
    visit(c, q)
  }
  seal(c)
}

/** The edges a node leaves by: every next for a fan-out, else the scripted or first route. */
function choose(c: Ctx, id: string, n: Node, v: number, route?: string): FlowEdge[] {
  const es = edgesOf(id, n).filter((e) => e.kind !== "error" && e.kind !== "max")
  if (es.every((e) => e.kind === "next")) return es
  if (route) return es.filter((e) => e.label === route).slice(0, 1)
  const want = c.run.script.choose?.[id]?.[v - 1]
  return [es.find((e) => e.slot === want) ?? es.find((e) => e.label === "approve") ?? es.find((e) => e.kind !== "next") ?? es[0]!]
}
const go = (c: Ctx, edges: FlowEdge[], at: number) => edges.forEach((e) => c.queue.push({ node: e.to, at: at + 250, via: [edgeKey(e)] }))

function visit(c: Ctx, q: Queued) {
  const { spec } = c.run
  const n = spec.nodes[q.node]
  if (!n) return
  if (n.join === "all") {
    const need = new Set(allEdges(spec).filter((e) => e.to === q.node).map((e) => e.from)).size
    const got = [...(c.arrivals.get(q.node) ?? []), ...q.via]
    if (got.length < need) return void c.arrivals.set(q.node, got)
    c.arrivals.delete(q.node)
    q = { ...q, via: got }
  }
  const v = (c.visits.get(q.node) ?? 0) + 1
  c.visits.set(q.node, v)
  const key = v > 1 ? `${q.node}#${v}` : q.node
  emit(c, q.at, "step.ready", key, "Engine", v > 1 ? `Visit ${v}` : "Ready", { node: q.node, visit: v, via: q.via })
  if (n.max_visits && v > n.max_visits) {
    emit(c, q.at + 100, "step.skipped", key, "Engine", `Visited ${n.max_visits} times`, { reason: `Visited ${n.max_visits} times` })
    const max = edgesOf(q.node, n).find((e) => e.kind === "max")
    if (max) go(c, [max], q.at + 100)
    return
  }
  exec(c, q.node, key, v, q.at + 300, 1)
}

function exec(c: Ctx, id: string, key: string, v: number, t: number, attempt: number) {
  const { spec, script, test } = c.run
  const n = spec.nodes[id]!
  const end = script.end?.node === id && (script.end.visit ?? 1) === v ? script.end.as : null
  if (test?.pinned.includes(id)) {
    emit(c, t, "step.started", key, "Engine", "Pinned output", { attempt })
    emit(c, t + 200, "step.succeeded", key, "Engine", "Reused pinned output", { reused: "pinned" })
    return go(c, choose(c, id, n, v), t + 200)
  }
  if (n.kind === "approval" || n.kind === "wait") return pause(c, id, key, n, v, t, end)
  if (n.kind === "for_each") return each(c, id, key, n, v, t)
  const pace = script.pace ?? 1
  const agent = n.kind === "agent" ? agentOf(spec, n) : null
  const lane = agent ? `${agent}:${n.session ?? id}` : null
  const session = agent ? `ses_${chain("", c.run.id + key + attempt).slice(0, 8)}` : null
  const actor = agent ? agentName(agent) : "Engine"
  const d = DUR[n.kind] * pace * (0.7 + 0.6 * rand(c.run.id + key))
  const cost = agent ? round2(0.06 + 0.24 * rand(c.run.id + key + "c")) : 0
  emit(c, t, "step.started", key, "Engine", `Attempt ${attempt}`, { attempt, session, lane, agent })
  if (agent) emit(c, t + 600, "step.brief_sent", key, "Engine", `Brief sent to ${actor}`)
  if (end && end !== "wait") return stop(c, id, key, n, t, d, end, { attempt, session, lane, agent, actor, cost })
  const at = t + d
  if (n.kind === "action" && toolWrites(n.tool)) {
    if (test && !test.live_writes) emit(c, at - 300, "proposal.recorded", key, "Engine", `Would send ${n.tool}`, { tool: n.tool, status: "would_send" })
    else emit(c, at - 300, "delivery.sent", key, "Engine", `${n.tool} done`, { tool: n.tool })
  }
  emit(c, at - 200, "step.output_recorded", key, actor, "Output recorded")
  const edges = choose(c, id, n, v)
  const routed = edges.length === 1 && edges[0]!.kind !== "next" ? edges[0]! : null
  if (routed) emit(c, at - 100, "step.route_chosen", key, actor, `Chose ${routed.label}`, { route: routed.label, slot: routed.slot })
  emit(c, at, "step.succeeded", key, "Engine", `Succeeded in ${Math.max(1, Math.round(d / S))}s`, { cost })
  if (agent) emit(c, at, "budget.checked", key, "Engine", `Within the $${n.budget_usd ?? spec.defaults?.budget_usd ?? 1} step budget`)
  if (n.kind === "finish") return finish(c, id, n, at)
  go(c, edges, at)
}

type Who = { attempt: number; session: string | null; lane: string | null; agent: string | null; actor: string; cost: number }
function stop(c: Ctx, id: string, key: string, n: Node, t: number, d: number, end: End, w: Who) {
  const by = c.run.by ?? c.run.owner
  let at = t + d * 0.5
  if (end === "fail") {
    emit(c, at, "step.retry_scheduled", key, "Engine", "Provider overloaded, retrying in 20s", { class: "retryable", message: "The model provider was overloaded.", cost: round2(w.cost / 2) })
    at += 20 * S * (c.run.script.pace ?? 1)
    emit(c, at, "step.started", key, "Engine", `Attempt ${w.attempt + 1}`, { attempt: w.attempt + 1, session: w.session, lane: w.lane, agent: w.agent })
    at += d * 0.8
    const field = Object.keys(outputFields(n))[0]
    const message = field ? `output.${field} is required` : `${n.tool ?? titleOf(id, n)} did not answer in 30s`
    if (field) emit(c, at, "step.contract_rejected", key, w.actor, "Output rejected", { errors: [message, "output has an unknown field: summary"] })
    emit(c, at + 300, "step.failed", key, "Engine", field ? "Output did not match the contract" : "Timed out twice", { class: field ? "contract" : "retryable", message, cost: w.cost })
    emit(c, at + 300, "run.failed", key, "Engine", `Failed at ${titleOf(id, n)}`, { reason: "step_failed", message })
    c.stop = at + 300
  } else if (end === "unknown") {
    const message = `The worker stopped during ${n.tool ?? "the step"}. It may have finished.`
    emit(c, at, "step.unknown", key, "Engine", "Outcome unknown", { message })
    emit(c, at, "run.waiting", null, "Engine", "Needs a decision", { reason: "unknown" })
  } else if (end === "cancel") {
    emit(c, at, "step.cancelled", key, by.name, "Cancelled", { cost: round2(w.cost / 2) })
    emit(c, at, "run.cancelled", null, by.name, `Cancelled by ${by.name}`, { by })
    c.stop = at
  } else {
    emit(c, at, "step.cancelled", key, "Engine", "Deadline reached")
    emit(c, at, "run.timed_out", null, "Engine", `Deadline ${c.run.spec.limits?.deadline ?? "2h"} reached`)
    c.stop = at
  }
}

function pause(c: Ctx, id: string, key: string, n: Node, v: number, t: number, end: End | null) {
  const { script, test } = c.run
  const approval = n.kind === "approval"
  const kind: WaitKind = approval ? "approval" : n.event ? "event" : "timer"
  const to = approval ? [test ? c.run.by ?? c.run.owner : c.run.owner] : []
  const after = durMs(typeof n.timeout === "string" ? n.timeout : n.timeout?.after) ?? durMs(n.for)
  const what = approval ? `Approval from ${to.map((p) => p.name).join(", ")}` : kind === "event" ? `Waiting for ${n.event!.source}` : "Waiting for a time"
  emit(c, t, "step.started", key, "Engine", "Started", { attempt: 1 })
  const attachments = approval && n.attach ? [{ name: "comparison.md", size: 4812, url: fileUrl(c.run, "comparison.md") }] : []
  emit(c, t, "wait.opened", key, "Engine", what, { id: waitId(c.run, key), kind, addressees: to, expires_at: after ? iso(t + after) : null, attachments })
  if (approval && (n.deliver === "dm" || n.deliver === "thread")) {
    const where = n.deliver === "dm" ? `Direct message to ${to[0]?.name ?? "the owner"}` : `Thread in #${n.channel ?? "sales"}`
    emit(c, t, "wait.posted", key, "Engine", `Posted in ${where}`, { sent: { platform: "discord", where, link: "https://discord.com/channels/745261709622771773/1/2", at: iso(t) } })
  }
  emit(c, t, "run.waiting", null, "Engine", approval ? "Waiting for approval" : what, { reason: kind })
  if (end === "wait") return void (c.stop = t)
  if (end === "timeout") {
    emit(c, t + 2 * H, "wait.timed_out", key, "Engine", "Timed out")
    emit(c, t + 2 * H, "step.cancelled", key, "Engine", "Deadline reached")
    emit(c, t + 2 * H, "run.timed_out", null, "Engine", `Deadline ${c.run.spec.limits?.deadline ?? "2h"} reached`)
    return void (c.stop = t + 2 * H)
  }
  if (test && approval) return void (c.stop = t)
  const wait = approval ? (script.decide_after ?? 25) * M : kind === "timer" ? 3 * H : 35 * M
  decide(c, id, key, n, v, t + wait * (script.pace ?? 1), approval ? c.run.owner : null)
}

export const waitId = (run: Run, key: string) => `wt_${run.id}_${key}`
export const fileUrl = (run: Run, name: string) => `/api/workflows/runs/${run.id}/files/${encodeURIComponent(name)}`

function decide(c: Ctx, id: string, key: string, n: Node, v: number, at: number, by: Person | null, route?: string, text?: string) {
  const edges = choose(c, id, n, v, route)
  const name = edges[0]?.kind === "next" ? null : edges[0]?.label ?? null
  const detail = by ? `${name === "approve" ? "Approved" : name === "reject" ? "Rejected" : cap(name ?? "Done")} by ${by.name}` : n.event ? "Event arrived" : "Time reached"
  const answer = by && text != null ? { by, via: "web", text, values: { text }, files: [], at: iso(at) } : null
  emit(c, at, "wait.decided", key, by?.name ?? "Engine", answer ? `Answered by ${by?.name}` : detail, { decision: name, by, answer })
  emit(c, at, "run.resumed", null, "Engine", "Resumed")
  if (name) emit(c, at, "step.route_chosen", key, by?.name ?? "Engine", `Chose ${name}`, { route: name, slot: edges[0]!.slot })
  emit(c, at, "step.succeeded", key, "Engine", "Succeeded")
  go(c, edges, at)
}

function itemsOf(run: Run, n: Node): string[] {
  const path = n.items?.match(/inputs\.(\w+)/)?.[1]
  const v = path ? run.inputs[path] : null
  return Array.isArray(v) ? v.map(String) : ["First", "Second", "Third"]
}

function each(c: Ctx, id: string, key: string, n: Node, v: number, t: number) {
  const items = itemsOf(c.run, n)
  const agent = agentOf(c.run.spec, n.do ?? n)
  const free = Array<number>(n.concurrency ?? 1).fill(t + 300)
  emit(c, t, "step.started", key, "Engine", `${items.length} items`, { attempt: 1, total: items.length })
  items.forEach((label, i) => {
    const slot = free.indexOf(Math.min(...free))
    const a = free[slot]!, k = `${key}[${i}]`
    const d = DUR.agent * (c.run.script.pace ?? 1) * (0.6 + 0.8 * rand(c.run.id + k))
    emit(c, a, "step.ready", k, "Engine", label, { node: id, visit: v, item: { index: i, label } })
    emit(c, a + 100, "step.started", k, "Engine", "Attempt 1", { attempt: 1, agent, lane: `${agent}:${k}`, session: `ses_${chain("", c.run.id + k).slice(0, 8)}` })
    emit(c, a + d, "step.output_recorded", k, agentName(agent), "Output recorded")
    emit(c, a + d, "step.succeeded", k, "Engine", `Succeeded in ${Math.round(d / S)}s`, { cost: round2(0.05 + 0.1 * rand(k + c.run.id)) })
    free[slot] = a + d + 150
  })
  const end = Math.max(...free)
  emit(c, end, "step.succeeded", key, "Engine", `${items.length} of ${items.length} done`)
  go(c, choose(c, id, n, v), end)
}

function finish(c: Ctx, id: string, n: Node, at: number) {
  const dl = finishDeliver(n)
  if (dl) {
    const to = c.run.test ? (c.run.by ?? c.run.owner).name : dl.to === "home" ? "home channel" : dl.to
    emit(c, at + 200, "delivery.queued", id, "Engine", `To ${to}`)
    emit(c, at + 900, "delivery.sent", id, "Engine", `Posted to ${to}`)
  }
  if (n.outcome === "failed") emit(c, at + 1000, "run.failed", id, "Engine", `Ended at ${titleOf(id, n)}`, { reason: "finish_failed", message: titleOf(id, n) })
  else emit(c, at + 1000, "run.succeeded", null, "Engine", "Succeeded", { outcome: /quiet|ignore/.test(id) ? "nothing_to_do" : "completed" })
  c.stop = at + 1000
}

// ---------- runs ----------

const TRIGGER_TEXT: Record<TriggerKind, string> = { manual: "Run by", schedule: "Schedule", webhook: "Webhook delivery", event: "Event", call: "Called by" }

/** Starts a run at `at`; a fork reuses the parent's finished upstream steps and starts at `from`. */
export function start(run: Run, at: number, fork?: { parent: Run; from: string }) {
  const c = ctx(run)
  const who = run.trigger.kind === "manual" ? (run.by ?? run.owner).name : "Engine"
  emit(c, at, "run.queued", null, who, run.trigger.kind === "manual" ? `${TRIGGER_TEXT.manual} ${who}` : `${TRIGGER_TEXT[run.trigger.kind]} ${run.delivery ?? run.trigger.id}`, { trigger: run.trigger })
  if (fork) emit(c, at, "run.forked_from", null, who, `From run ${fork.parent.number}`, { run: fork.parent.id, number: fork.parent.number, step: fork.from })
  emit(c, at + 400, "run.started", null, "Engine", run.test ? "Test run on the draft" : `Version ${run.version}`)
  if (!fork) {
    c.queue.push({ node: run.spec.start, at: at + 700, via: [] })
    return drive(c)
  }
  const up = ancestors(run.spec, fork.from)
  const prev = project(visible(fork.parent))
  let via: string[] = []
  for (const s of prev.steps) {
    if (s.item) continue
    if (s.node === fork.from && !via.length) via = s.via
    if (!up.has(s.node) || s.status !== "succeeded" || c.visits.has(s.node)) continue
    c.visits.set(s.node, 1)
    emit(c, at + 500, "step.ready", s.node, "Engine", "Ready", { node: s.node, visit: 1, via: s.via })
    emit(c, at + 500, "step.succeeded", s.node, "Engine", `Reused from run ${fork.parent.number}`, { reused: "fork" })
  }
  c.queue.push({ node: fork.from, at: at + 700, via })
  drive(c)
}

export const visible = (run: Run, now = Date.now()) => {
  const i = run.events.findIndex((e) => ms(e.at) > now)
  return i < 0 ? run.events : run.events.slice(0, i)
}
/** Drops rows not yet released: a decision now replaces what the script planned. */
const cut = (run: Run, now: number) => void (run.events = visible(run, now))

export function cancel(run: Run, by: Person, now = Date.now()) {
  cut(run, now)
  const c = ctx(run)
  for (const s of project(run.events).steps) {
    if (["ready", "running", "waiting", "unknown"].includes(s.status)) emit(c, now, "step.cancelled", s.key, by.name, "Cancelled")
  }
  emit(c, now, "run.cancelled", null, by.name, `Cancelled by ${by.name}`, { by })
  seal(c)
}

/** Retry from failure: the failed step runs again with a new attempt; finished steps stay. */
export function redrive(run: Run, by: Person, now = Date.now()) {
  cut(run, now)
  const st = project(run.events)
  const step = st.steps.find((s) => s.key === st.failure?.step) ?? st.steps.findLast((s) => s.status === "failed" || s.status === "cancelled")
  if (!step) return
  run.script = { ...run.script, end: undefined }
  const c = ctx(run)
  emit(c, now, "run.redriven", null, by.name, `Retry from ${step.key} by ${by.name}`, { from: step.key, by })
  retry(c, step.node, step.key, step.visit, step.attempts.length, now)
}
function retry(c: Ctx, node: string, key: string, visit: number, attempts: number, now: number) {
  emit(c, now + 200, "step.ready", key, "Engine", "Ready", { node, visit })
  exec(c, node, key, visit, now + 500, attempts + 1)
  drive(c)
}

export type Decision = { decision: "retry" | "skip" | "fail"; output?: unknown }
/** Settles a step whose outcome is unknown. */
export function decideUnknown(run: Run, d: Decision, by: Person, now = Date.now()) {
  cut(run, now)
  const st = project(run.events)
  const step = st.steps.find((s) => s.status === "unknown")
  if (!step) return false
  run.script = { ...run.script, end: undefined }
  const c = ctx(run)
  const words = { retry: "Retry", skip: "Skip with output", fail: "Fail" }[d.decision]
  emit(c, now, "step.resolved_by", step.key, by.name, `${words} by ${by.name}`, { decision: d.decision, by, output: d.output ?? null })
  if (d.decision === "fail") {
    emit(c, now, "step.failed", step.key, by.name, `Failed by ${by.name}`, { class: "unknown", message: `Failed by ${by.name}` })
    emit(c, now, "run.failed", step.key, "Engine", "Step failed", { reason: "unknown_step", message: `Failed by ${by.name}` })
    seal(c)
    return true
  }
  emit(c, now, "run.resumed", null, "Engine", "Resumed")
  if (d.decision === "retry") return retry(c, step.node, step.key, step.visit, step.attempts.length, now), true
  const n = run.spec.nodes[step.node]!
  emit(c, now + 100, "step.succeeded", step.key, by.name, "Skipped with output", { skipped_with: d.output ?? null })
  go(c, choose(c, step.node, n, step.visit), now + 100)
  drive(c)
  return true
}

/** Decides an open approval with one of its routes. */
export function decideWait(run: Run, key: string, route: string, by: Person, now = Date.now(), text?: string) {
  cut(run, now)
  const s = project(run.events).steps.find((x) => x.key === key && x.status === "waiting")
  if (!s) return false
  const c = ctx(run)
  decide(c, s.node, key, run.spec.nodes[s.node]!, s.visit, now, by, route, text)
  drive(c)
  return true
}

// ---------- read models ----------

export function summaryOf(run: Run, now = Date.now()): RunSummary {
  const ev = visible(run, now)
  const st = project(ev)
  const started = ev[0]?.at ?? iso(now)
  const at = (st.current_step ?? st.failure?.step)?.replace(/[#[].*$/, "")
  return {
    id: run.id, number: run.number, status: st.status, label: run.label, trigger: run.trigger, started_at: started,
    duration_ms: st.ended_at ? ms(st.ended_at) - ms(started) : null, current_step: st.current_step ?? st.failure?.step ?? null, cost_usd: st.cost_usd,
    step_title: at ? titleOf(at, run.spec.nodes[at]) : null,
    version: run.version, test: !!run.test, workflow: run.wf, waiting_reason: st.waiting_reason, triggered_by: run.trigger.kind === "manual" ? run.by ?? run.owner : null,
    redriven: ev.some((e) => e.type === "run.redriven"),
  }
}

export function detailOf(run: Run, now = Date.now()): RunDetail {
  const ev = visible(run, now)
  const agents = Object.values(run.spec.nodes).filter((n) => n.kind === "agent" || n.kind === "for_each").length
  const sum = summaryOf(run, now)
  const deadline = durMs(run.spec.limits?.deadline)
  return {
    ...sum, ...project(ev), spec: run.spec, inputs: run.inputs, untrusted_inputs: run.trigger.kind === "webhook", parent: run.parent,
    delivery: run.delivery, test_options: run.test, budget_usd: run.spec.limits?.budget_usd ?? (run.spec.defaults?.budget_usd ?? 1) * Math.max(agents, 1),
    deadline_at: deadline ? iso(ms(sum.started_at) + deadline) : null, events: ev.length, head_hash: ev.at(-1)?.hash ?? GENESIS,
  }
}

// ---------- step detail ----------

const SAMPLE: Record<string, unknown> = {
  company: "Brightwave Telecom", fit: "strong", notes: "1,200 staff, runs 40 regional stores. Wants promotion forecasting before Q1.",
  subject: "Forecasting promotions at Brightwave", body: "Hi Ana,\n\nThanks for reaching out. Forecasting promotions across 40 stores is close to what we do every week.\nWould a 30 minute call on Tuesday or Thursday work?\n\nBest,\nMaya",
  title: "Priors for media mix models", points: ["Open with the business question", "Cut the second section by half", "Name the prior in the example"],
  severity: "medium", cause: "Deploy 4512 raised p99 latency on the search API.", digest: "3 deals at risk: Larkspur (no activity in 21 days), ...",
  report: "| Competitor | Pricing | Position |\n| --- | --- | --- |\n| Northwind | per seat | mid-market |", brief: "Kickoff Thursday. Owner Jonas. Data access pending.",
  summary: "Weekly readout posted.", category: "bug", reply: "Thanks, we reproduced it and opened an issue.", message_id: "msg_18c4f2a9",
}
function sample(name: string, f: Field): unknown {
  if (name in SAMPLE) return SAMPLE[name]
  if (f.enum) return f.enum[0]
  if (f.type === "boolean") return true
  if (f.type === "number" || f.type === "integer") return 42
  if (f.type === "array") return [sample(`${name}_item`, f.items ?? { type: "string" }), sample(`${name}_item`, f.items ?? { type: "string" })]
  if (f.type === "object") return { id: "rec_3f9a" }
  return `${cap(name.replace(/_/g, " "))} text`
}
function outputOf(run: Run, node: string, item?: string): unknown {
  const n = run.spec.nodes[node]
  if (!n) return null
  if (item != null) return { name: item, pricing: "Per seat, from $49", positioning: "Mid-market analytics", customers: ["Tidewater Foods", "Cobalt"] }
  if (n.kind === "action") return { ok: true, id: n.tool?.startsWith("gmail") ? SAMPLE.message_id : "rec_7c21" }
  if (n.kind === "approval") return { decision: "approve", body: SAMPLE.body }
  return Object.fromEntries(Object.entries(outputFields(n)).map(([k, f]) => [k, sample(k, f)]))
}

/** Resolves `${{ ... }}` expressions against the run's inputs and earlier outputs. */
function resolve(run: Run, expr: string, item?: string): unknown {
  const m = expr.match(/^\$\{\{\s*(.+?)\s*\}\}$/)
  if (!m) return expr
  const path = m[1]!.split(".")
  if (path[0] === "item") return item
  if (path[0] === "inputs") return path.length > 1 ? run.inputs[path[1]!] : run.inputs
  if (path[0] === "steps" && path[1]) {
    const out = outputOf(run, path[1]) as Record<string, unknown> | null
    return path[3] ? out?.[path[3]] : out
  }
  return expr
}

const TOOLS: Record<string, string[]> = {
  sales: ["web/search", "hubspot/search_crm", "hubspot/get_object"], daimon: ["web/search", "notion/search", "web/fetch"],
  mmm: ["warehouse/query", "notebook/run"], pricing: ["warehouse/query", "notebook/run"], reviewer: ["github/get_pull", "github/get_diff"],
}

export function stepDetail(run: Run, key: string, attempt: number | null, now = Date.now()): StepDetail | null {
  const ev = visible(run, now)
  const st = project(ev).steps.find((s) => s.key === key)
  if (!st) return null
  const node = run.spec.nodes[st.node]!
  const n = st.item ? node.do ?? node : node
  const a = st.attempts[(attempt ?? st.attempts.length) - 1] ?? null
  const agent = n.kind === "agent" ? agentOf(run.spec, n) : null
  const mine = ev.filter((e) => e.step === key)
  const exprs = n.kind === "agent" ? n.inputs ?? {} : n.kind === "action" ? n.args ?? {} : {}
  const input = Object.fromEntries(Object.entries(exprs).map(([k, e]) => [k, resolve(run, e, st.item?.label)]))
  const untrusted = !run.trigger || run.trigger.kind !== "webhook" ? [] : Object.entries(exprs).flatMap(([k, e]) =>
    /^\$\{\{\s*inputs\s*\}\}$/.test(e) ? Object.keys(run.inputs).map((f) => `input.${k}.${f}`) : /inputs\./.test(e) ? [`input.${k}`] : [])
  const done = st.status === "succeeded" && (!a || a.status === "succeeded")
  const skipped = mine.find((e) => e.type === "step.succeeded" && "skipped_with" in e.data)
  const output = skipped ? skipped.data.skipped_with : done ? outputOf(run, st.node, st.item?.label) : null
  const routeDef = st.route ? node.routes?.[st.route] : null
  const reason = typeof routeDef === "object" && routeDef?.when ? routeDef.when : st.route ? "Matched the route condition." : ""
  const proposals: Proposal[] = mine.filter((e) => e.type === "proposal.recorded" || e.type === "delivery.sent").map((e, i) => ({
    id: `prop_${i + 1}`, tool: String(e.data.tool ?? n.tool ?? "delivery"), summary: e.type === "proposal.recorded" ? describe(n, input) : e.detail,
    status: e.type === "proposal.recorded" ? "would_send" : "sent",
  }))
  const t0 = a ? ms(a.started_at) : null, t1 = a?.duration_ms != null ? t0! + a.duration_ms : now
  const lines: [number, ActivityLine["kind"], string, string?][] = agent ? [
    [0.05, "message", "Reading the brief and inputs"], [0.2, "tool", "Searching", TOOLS[agent]?.[0]], [0.42, "tool", "Looking up the record", TOOLS[agent]?.[1] ?? TOOLS[agent]?.[0]],
    [0.6, "message", "Comparing what I found with the instructions"], [0.8, "tool", "Checking a detail", TOOLS[agent]?.[2] ?? TOOLS[agent]?.[0]], [0.95, "note", "Recording the output"],
  ] : n.tool ? [[0.1, "tool", `Calling ${n.tool}`, n.tool], [0.9, "note", "Response recorded"]] : []
  const activity = t0 == null ? [] : lines.map(([f, kind, text, tool]) => ({ at: iso(t0 + f * (a?.duration_ms ?? DUR.agent)), kind, text, tool }))
    .filter((l) => ms(l.at) <= t1 && ms(l.at) <= now)
  const prog = t0 == null ? 0 : a?.status === "running" ? (now - t0) / DUR.agent : 1
  const plan = agent ? ["Read the inputs", "Gather facts with tools", "Check against the instructions", "Write the output"].map((text, i) => ({
    text, status: prog >= (i + 1) / 4 ? "done" as const : prog >= i / 4 ? "active" as const : "todo" as const,
  })) : []
  return {
    key, attempt: a?.n ?? 0, agent: agent ? agentName(agent) : null, model: agent ? n.model ?? AGENTS.find((a) => a.id === CATALOG.agents.find((x) => x.key === agent)?.id)?.model ?? null : null,
    summary: agent && done ? summaryText(st.node, output) : null, route: st.route ? { name: st.route, reason } : null,
    files: st.node === "synthesise" && done ? [{ name: "comparison.md", size: 4812, url: fileUrl(run, "comparison.md") }] : [],
    input, output, untrusted,
    rejections: mine.filter((e) => e.type === "step.contract_rejected").map((e) => ({ attempt: st.attempts.findLastIndex((x) => ms(x.started_at) <= ms(e.at)) + 1, errors: e.data.errors as string[] })),
    proposals, activity, plan,
  }
}
function describe(n: Node, input: Record<string, unknown>) {
  if (n.tool?.startsWith("gmail")) return `Email to ${String(input.to ?? "")}: ${String(input.subject ?? "")}`
  return `${n.tool} with ${Object.keys(input).join(", ")}`
}
function summaryText(node: string, out: unknown) {
  const o = (out ?? {}) as Record<string, unknown>
  if (node === "research") return `${String(o.company)} looks like a strong fit: retail, 40 stores, asking about promotion forecasts. No prior contact in HubSpot.`
  if (node === "draft_reply") return "Drafted a four line reply that thanks Ana, mirrors the store count and proposes two call slots."
  return "Read the inputs, gathered what the instructions asked for and recorded the output."
}

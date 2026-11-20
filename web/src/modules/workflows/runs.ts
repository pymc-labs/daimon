// Run state from the ledger: the projection the run page renders, edge states for the graph and the timeline bars.
import { allEdges, edgeKey } from "./spec"
import type { Answer, AttemptStatus, ErrorClass, FileRef, Person, RunEvent, RunState, RunStatus, Spec, StepRun, StepStatus, Sent, WaitingReason, WaitKind } from "./types"

const ms = (iso: string) => new Date(iso).getTime()
export const ENDED_STEP: StepStatus[] = ["succeeded", "failed", "skipped", "cancelled"]
export const ACTIVE_RUN: RunStatus[] = ["queued", "running", "waiting"]
const str = (v: unknown) => (typeof v === "string" ? v : null)

/** Folds ledger rows, oldest first, into the run's state. Replaying a prefix gives the state at that row. */
export function project(events: RunEvent[]): RunState {
  const s: RunState = { status: "queued", waiting_reason: null, failure: null, outcome: null, ended_at: null, cost_usd: 0, steps: [], lanes: [], current_step: null }
  const by = new Map<string, StepRun>()
  const end = (status: RunStatus, at: string) => Object.assign(s, { status, waiting_reason: null, ended_at: at })
  for (const e of events) {
    const d = e.data
    const st = e.step ? by.get(e.step) : undefined
    const last = st?.attempts.at(-1)
    const cost = typeof d.cost === "number" ? d.cost : 0
    if (cost) {
      s.cost_usd += cost
      if (st) st.cost_usd += cost
      if (last) last.cost_usd += cost
    }
    const close = (status: AttemptStatus, err?: { class: ErrorClass; message: string }) => {
      if (last?.status !== "running") return
      Object.assign(last, { status, duration_ms: ms(e.at) - ms(last.started_at), error: err ?? null })
    }
    const settle = (status: StepStatus) => st && Object.assign(st, { status, ended_at: e.at })
    const err = { class: (str(d.class) ?? "agent") as ErrorClass, message: str(d.message) ?? e.detail }
    switch (e.type) {
      case "run.started": case "run.resumed": case "run.redriven":
        Object.assign(s, { status: "running", waiting_reason: null, failure: null, ended_at: null })
        break
      case "run.waiting":
        Object.assign(s, { status: "waiting", waiting_reason: str(d.reason) as WaitingReason })
        break
      case "run.succeeded":
        end("succeeded", e.at).outcome = d.outcome === "nothing_to_do" ? "nothing_to_do" : "completed"
        break
      case "run.failed":
        end("failed", e.at).failure = { reason: str(d.reason) ?? "step_failed", message: str(d.message) ?? e.detail, step: e.step ?? str(d.step) }
        break
      case "run.cancelled": end("cancelled", e.at); break
      case "run.timed_out": end("timed_out", e.at); break
      case "step.ready": {
        if (st) {
          Object.assign(st, { status: "ready", ended_at: null, reason: null })
          break
        }
        const item = d.item as StepRun["item"] | undefined
        const n: StepRun = {
          key: e.step!, node: str(d.node) ?? e.step!, visit: typeof d.visit === "number" ? d.visit : 1, item: item ?? null, status: "ready",
          started_at: null, ended_at: null, cost_usd: 0, route: null, attempts: [], wait: null, lane: null, progress: null, reason: null,
          via: Array.isArray(d.via) ? (d.via as string[]) : [], reused: null,
        }
        by.set(n.key, n)
        s.steps.push(n)
        break
      }
      case "step.started": {
        if (!st) break
        st.status = "running"
        st.started_at ??= e.at
        st.reason = null
        st.attempts.push({ n: st.attempts.length + 1, status: "running", started_at: e.at, duration_ms: null, cost_usd: 0, session: str(d.session), error: null })
        if (typeof d.total === "number") st.progress = { done: 0, total: d.total }
        const lane = str(d.lane)
        if (lane) {
          st.lane = lane
          if (!s.lanes.some((l) => l.id === lane)) s.lanes.push({ id: lane, agent: str(d.agent) ?? "", session: str(d.session) ?? "" })
        }
        break
      }
      case "step.route_chosen": if (st) st.route = str(d.route); break
      case "step.retry_scheduled": close("failed", err); break
      case "step.succeeded":
        close("succeeded")
        settle("succeeded")
        if (st && d.reused) st.reused = d.reused === "pinned" ? "pinned" : "fork"
        break
      case "step.failed": close("failed", err); settle("failed"); if (st) st.reason = err.message; break
      case "step.unknown":
        close("abandoned", { class: "unknown", message: err.message })
        if (st) Object.assign(st, { status: "unknown", reason: err.message })
        break
      case "step.skipped": settle("skipped"); if (st) st.reason = str(d.reason); break
      case "step.cancelled":
        close("abandoned")
        settle("cancelled")
        if (st?.wait?.status === "waiting") Object.assign(st.wait, { status: "cancelled", closed_at: e.at })
        break
      case "step.resolved_by": if (st) st.reason = null; break
      case "wait.opened":
        if (!st) break
        st.status = "waiting"
        st.wait = {
          id: str(d.id) ?? "", kind: (str(d.kind) ?? "approval") as WaitKind, status: "waiting", addressees: (d.addressees as Person[] | undefined) ?? [],
          opened_at: e.at, expires_at: str(d.expires_at), closed_at: null, decision: null, decider: null,
          sent: [], attachments: (d.attachments as FileRef[] | undefined) ?? [], answer: null,
        }
        break
      case "wait.posted":
        if (st?.wait) st.wait.sent = [...st.wait.sent, d.sent as Sent]
        break
      case "wait.decided":
        if (st?.wait) Object.assign(st.wait, { status: "completed", closed_at: e.at, decision: str(d.decision), decider: (d.by as Person | undefined) ?? null, answer: (d.answer as Answer | undefined) ?? null })
        break
      case "wait.timed_out":
        if (st?.wait) Object.assign(st.wait, { status: "timed_out", closed_at: e.at, decision: str(d.route) })
        break
      default:
    }
  }
  for (const p of s.steps) {
    if (!p.progress) continue
    const items = s.steps.filter((x) => x.item && x.key.startsWith(`${p.key}[`))
    p.progress = { total: p.progress.total, done: items.filter((x) => ENDED_STEP.includes(x.status)).length }
  }
  const live = s.steps.filter((x) => !x.item && (x.status === "running" || x.status === "waiting" || x.status === "unknown"))
  s.current_step = (live.find((x) => x.status === "unknown") ?? live.at(-1))?.key ?? null
  s.cost_usd = Math.round(s.cost_usd * 100) / 100
  return s
}

export type NodeRun = { status: StepStatus; visits: number; key: string; progress: StepRun["progress"]; reused: StepRun["reused"]; ms: number | null }

/** The latest visit per node (For each items roll up into their node). */
export function nodeRuns(steps: StepRun[]): Record<string, NodeRun> {
  const out: Record<string, NodeRun> = {}
  for (const s of steps) {
    if (s.item) continue
    const took = s.started_at && s.ended_at ? ms(s.ended_at) - ms(s.started_at) : null
    out[s.node] = { status: s.status, visits: (out[s.node]?.visits ?? 0) + 1, key: s.key, progress: s.progress, reused: s.reused, ms: took }
  }
  return out
}

export type EdgeState = "taken" | "not_taken" | "pending"

/** Taken once a step was reached through it; not taken once its source ended (or the run did) without using it. */
export function edgeStates(spec: Spec, steps: StepRun[], runEnded: boolean): Record<string, EdgeState> {
  const taken = new Set(steps.flatMap((s) => s.via))
  const done = new Set(steps.filter((s) => !s.item && ENDED_STEP.includes(s.status)).map((s) => s.node))
  const out: Record<string, EdgeState> = {}
  for (const e of allEdges(spec)) {
    const k = edgeKey(e)
    out[k] = taken.has(k) ? "taken" : runEnded || done.has(e.from) ? "not_taken" : "pending"
  }
  return out
}

export type Bar = { left: number; width: number; kind: "work" | "wait" }
export type TimelineRow = { key: string; bars: Bar[] }
const MIN_W = 0.004

/** Bars against wall-clock time, as fractions of [from, to]; waits get their own segment. */
export function timeline(steps: StepRun[], from: number, to: number): TimelineRow[] {
  const span = Math.max(to - from, 1)
  const frac = (t: number) => Math.min(Math.max((t - from) / span, 0), 1)
  const bar = (a: number, b: number, kind: Bar["kind"]): Bar => ({ left: frac(a), width: Math.max(frac(b) - frac(a), MIN_W), kind })
  return steps.map((s) => {
    if (!s.started_at) return { key: s.key, bars: [] }
    const a = ms(s.started_at), b = s.ended_at ? ms(s.ended_at) : to
    const w = s.wait
    if (!w) return { key: s.key, bars: [bar(a, b, "work")] }
    const wa = ms(w.opened_at), wb = w.closed_at ? ms(w.closed_at) : b
    const bars = [bar(wa, wb, "wait")]
    if (wa - a > 500) bars.unshift(bar(a, wa, "work"))
    if (b - wb > 500) bars.push(bar(wb, b, "work"))
    return { key: s.key, bars }
  })
}

/** Evenly spaced axis ticks for the timeline: the step in ms and the marks in [from, to]. */
export function axis(from: number, to: number, want = 6): number[] {
  const span = Math.max(to - from, 1)
  const steps = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400].map((s) => s * 1000)
  const step = steps.find((s) => span / s < want) ?? 86_400_000
  const out: number[] = []
  for (let t = Math.ceil(from / step) * step; t <= to; t += step) out.push(t)
  return out
}

/** Ledger rows grouped by step in order of first appearance; run-level rows group under null. */
export function groupByStep(events: RunEvent[]): { step: string | null; events: RunEvent[] }[] {
  const out: { step: string | null; events: RunEvent[] }[] = []
  for (const e of events) {
    const prev = out.at(-1)
    if (prev && prev.step === e.step) prev.events.push(e)
    else out.push({ step: e.step, events: [e] })
  }
  return out
}

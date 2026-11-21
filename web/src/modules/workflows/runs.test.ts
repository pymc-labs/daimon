import { describe, expect, it } from "vitest"

import { cancel, decideUnknown, redrive, start, verify, visible, type Run, type Script } from "@/mock/workflows-runs"
import { SPECS } from "@/mock/workflows-specs"
import { axis, edgeStates, nodeRuns, project, timeline } from "./runs"
import type { RunEvent, Spec } from "./types"

const T = Date.parse("2026-11-13T09:00:00Z")
const maya = { id: "p_maya", name: "Maya Lindqvist" }
function run(spec: Spec, script: Script = {}, inputs: Record<string, unknown> = {}): Run {
  const r: Run = {
    id: "run_1", wf: { id: "wf", name: spec.name }, number: 1, spec, version: 1, inputs, label: null, trigger: { kind: "manual", id: "manual" },
    delivery: null, by: maya, owner: maya, test: null, parent: null, script, events: [],
  }
  start(r, T)
  return r
}
const end = (r: Run) => Date.parse(r.events.at(-1)!.at)

describe("project", () => {
  it("folds a finished run into steps, routes and cost", () => {
    const s = project(run(SPECS.lead).events)
    expect(s.status).toBe("succeeded")
    expect(s.steps.map((x) => x.key)).toEqual(["research", "draft_reply", "review", "send", "done"])
    expect(s.steps[0]!.route).toBe("qualified")
    expect(s.steps[2]!.wait).toMatchObject({ kind: "approval", status: "completed", decision: "approve" })
    expect(s.cost_usd).toBeGreaterThan(0)
  })

  it("lists the lanes steps start in, and a new lane's session from its first brief", () => {
    const s = project(run(SPECS.lead).events)
    expect(s.lanes.map((l) => l.id)).toEqual(["main:sales"])
    expect(s.steps.filter((x) => x.lane).map((x) => x.key)).toEqual(["research", "draft_reply"])
    const ev = (seq: number, type: RunEvent["type"], data: RunEvent["data"]): RunEvent =>
      ({ seq, at: "2026-11-13T09:00:00Z", type, step: "a", actor: "Engine", detail: "", data, hash: "" })
    const brief = project([
      ev(1, "step.ready", { node: "a", visit: 1 }),
      ev(2, "step.started", { attempt: 1, lane: "main:x", agent: "x", session: null }),
      ev(3, "step.brief_sent", { session: "ses_1" }),
    ])
    expect(brief.lanes).toEqual([{ id: "main:x", agent: "x", session: "ses_1" }])
  })

  it("keeps a failed step's attempts with their error classes", () => {
    const s = project(run(SPECS.lead, { end: { node: "research", as: "fail" } }).events)
    expect(s.status).toBe("failed")
    expect(s.failure?.step).toBe("research")
    expect(s.steps[0]!.attempts.map((a) => a.error?.class)).toEqual(["retryable", "contract"])
  })

  it("counts loop visits and For each progress", () => {
    const loop = project(run(SPECS.content, { choose: { critique: ["routes.revise", "routes.accept"] } }).events)
    expect(nodeRuns(loop.steps).write!.visits).toBe(2)
    expect(loop.steps.map((x) => x.key)).toContain("write#2")
    const r = run(SPECS.research, {}, { competitors: ["a", "b", "c", "d", "e", "f"] })
    const items = r.events.filter((e) => e.type === "step.succeeded" && e.step?.startsWith("each["))
    const mid = project(visible(r, Date.parse(items[2]!.at)))
    expect(mid.steps.find((x) => x.key === "each")!.progress).toEqual({ done: 3, total: 6 })
    expect(mid.status).toBe("running")
  })
})

describe("edgeStates", () => {
  it("marks taken routes solid and the rest not taken once the source ends", () => {
    const r = run(SPECS.lead)
    const done = project(r.events)
    const e = edgeStates(SPECS.lead, done.steps, true)
    expect(e["research:routes.qualified"]).toBe("taken")
    expect(e["research:routes.unqualified"]).toBe("not_taken")
    expect(e["review:routes.reject"]).toBe("not_taken")
    const early = project(r.events.slice(0, 4))
    expect(edgeStates(SPECS.lead, early.steps, false)["review:routes.approve"]).toBe("pending")
  })
})

describe("recovery", () => {
  it("retries from failure with a new attempt on the same step", () => {
    const r = run(SPECS.lead, { end: { node: "draft_reply", as: "fail" } })
    redrive(r, maya, end(r) + 60_000)
    const s = project(r.events)
    expect(s.status).toBe("succeeded")
    expect(s.steps.filter((x) => x.node === "draft_reply")).toHaveLength(1)
    expect(s.steps.find((x) => x.key === "draft_reply")!.attempts.map((a) => a.status)).toEqual(["failed", "failed", "succeeded"])
  })

  it("settles an unknown step by skipping with output", () => {
    const r = run(SPECS.lead, { end: { node: "send", as: "unknown" } })
    expect(project(r.events)).toMatchObject({ status: "waiting", waiting_reason: "unknown", current_step: "send" })
    decideUnknown(r, { decision: "skip", output: { ok: true } }, maya, end(r) + 1000)
    expect(project(r.events).status).toBe("succeeded")
  })

  it("lets parallel branches finish around an unknown step and joins after retry", () => {
    const r = run(SPECS.onboard, { end: { node: "folder", as: "unknown" } })
    const s = project(r.events)
    expect(s).toMatchObject({ status: "waiting", waiting_reason: "unknown", current_step: "folder" })
    expect(s.steps.filter((x) => x.node === "page" || x.node === "project").map((x) => x.status)).toEqual(["succeeded", "succeeded"])
    decideUnknown(r, { decision: "retry" }, maya, end(r) + 1000)
    const after = project(r.events)
    expect(after.steps.find((x) => x.node === "welcome")?.status).toBe("succeeded")
  })

  it("cancels what is in flight and verifies the chain", () => {
    const r = run(SPECS.lead)
    cancel(r, maya, T + 20_000)
    const s = project(r.events)
    expect(s.status).toBe("cancelled")
    expect(s.steps.every((x) => x.status !== "running")).toBe(true)
    expect(verify(r.events)).toBe(true)
    r.events[3] = { ...r.events[3]!, detail: "edited" }
    expect(verify(r.events)).toBe(false)
  })
})

describe("timeline", () => {
  it("splits a step around its wait and clamps to the window", () => {
    const steps = project(run(SPECS.lead).events).steps
    const from = Date.parse(steps[0]!.started_at!), to = Date.parse(steps.at(-1)!.ended_at!)
    const rows = timeline(steps, from, to)
    expect(rows[0]!.bars[0]!.left).toBe(0)
    expect(rows.find((x) => x.key === "review")!.bars.map((b) => b.kind)).toEqual(["wait"])
    for (const b of rows.flatMap((x) => x.bars)) expect(b.left + b.width).toBeLessThanOrEqual(1.0041)
  })

  it("picks a readable axis step", () => {
    expect(axis(0, 60_000)).toEqual([0, 15_000, 30_000, 45_000, 60_000])
    expect(axis(0, 3_600_000).length).toBeLessThanOrEqual(6)
  })
})

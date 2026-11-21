import { describe, expect, it } from "vitest"

import { start, visible, type Run } from "@/mock/workflows-runs"
import { SPECS } from "@/mock/workflows-specs"
import { edgeStates, nodeRuns, project } from "../runs"
import type { RunDetail } from "../types"
import { listeningAt, monthWeeks, nodeOf, pendingNodes, shiftMonth, until } from "./lib"
import { answerRows } from "./rows"

const T = Date.parse("2026-11-13T09:00:00Z")
const maya = { id: "p_maya", name: "Maya Lindqvist" }

function detail(now: number): RunDetail {
  const r: Run = {
    id: "run_1", wf: { id: "wf", name: "Lead" }, number: 1, spec: SPECS.lead, version: 1, inputs: {}, label: null, trigger: { kind: "manual", id: "manual" },
    delivery: null, by: maya, owner: maya, test: null, parent: null, script: {}, events: [],
  }
  start(r, T)
  return { ...project(visible(r, now)), spec: SPECS.lead } as unknown as RunDetail
}
const overlay = (d: RunDetail) => ({ nodes: nodeRuns(d.steps), edges: edgeStates(d.spec, d.steps, false), started: true })

describe("listeningAt", () => {
  it("is open until the listen ends, then closed without a refetch", () => {
    const end = new Date(T + 60_000).toISOString()
    expect(listeningAt(end, T)).toBe(true)
    expect(listeningAt(end, T + 60_000)).toBe(false)
    expect(listeningAt(null, T)).toBe(false)
  })
})

describe("until", () => {
  it("formats minutes, hours and days left, never negative", () => {
    expect(until(new Date(T + 5 * 60_000).toISOString(), T)).toBe("5m")
    expect(until(new Date(T + 125 * 60_000).toISOString(), T)).toBe("2h 5m")
    expect(until(new Date(T + 26 * 3_600_000).toISOString(), T)).toBe("1d 2h")
    expect(until(new Date(T - 60_000).toISOString(), T)).toBe("0m")
  })
})

describe("nodeOf", () => {
  it("strips visit and item suffixes", () => {
    expect(["write", "write#2", "write[3]"].map(nodeOf)).toEqual(["write", "write", "write"])
  })
})

describe("pendingNodes", () => {
  it("lists nodes still reachable while the run is live", () => {
    const d = detail(T + 1000)
    expect(d.status).toBe("running")
    const p = pendingNodes(d.spec, d, overlay(d))
    expect(p).toContain("send")
    expect(p).not.toContain(d.steps[0]!.node)
  })

  it("is empty once the run ended", () => {
    const d = detail(T + 365 * 86_400_000)
    expect(d.status).toBe("succeeded")
    expect(pendingNodes(d.spec, d, overlay(d))).toEqual([])
  })
})

describe("answerRows", () => {
  it("shows a question's fields under their titles, in the order asked", () => {
    const rows = answerRows({
      by: maya, via: "web", text: null, values: { note: "Monday", channel: "#leads" }, files: [], at: "2026-11-13T09:00:00Z",
      labels: { channel: "Channel", note: "Note" },
    })
    expect(rows.filter(([, v]) => v).map(([k]) => k)).toEqual(["Answered by", "Channel", "Note"])
  })
})

describe("month grid", () => {
  it("lays a month out in Monday-first weeks, null outside the month", () => {
    const weeks = monthWeeks("2026-11")
    expect(weeks[0]).toEqual([null, null, null, null, null, null, "2026-11-01"])
    expect(weeks.at(-1)).toEqual(["2026-11-30", null, null, null, null, null, null])
    expect(weeks.every((w) => w.length === 7)).toBe(true)
    expect(weeks.flat().filter(Boolean)).toHaveLength(30)
  })

  it("moves across year ends", () => {
    expect(shiftMonth("2026-12", 1)).toBe("2027-01")
    expect(shiftMonth("2026-01", -1)).toBe("2025-12")
  })
})

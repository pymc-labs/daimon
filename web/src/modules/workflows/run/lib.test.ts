import { describe, expect, it } from "vitest"

import { start, visible, type Run } from "@/mock/workflows-runs"
import { SPECS } from "@/mock/workflows-specs"
import { edgeStates, nodeRuns, project } from "../runs"
import type { RunDetail } from "../types"
import { nodeOf, pendingNodes, until } from "./lib"

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

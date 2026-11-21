import { describe, expect, it } from "vitest"

import { SPECS } from "@/mock/workflows-specs"
import { layout, TRIGGERS } from "./layout"

const overlap = (a: { x: number; y: number; w: number; h: number }, b: typeof a) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h

describe("layout", () => {
  it("draws a review loop as one dashed arc right of its steps", () => {
    const l = layout(SPECS.content, ["Manual"])
    const back = l.edges.filter((e) => e.back)
    expect(back.map((e) => [e.from, e.to])).toEqual([["critique", "write"]])
    const at = Object.fromEntries(l.nodes.map((b) => [b.id, b]))
    const arcX = back[0]!.points[1]!.x
    expect(arcX).toBeGreaterThan(Math.max(at.write!.x + at.write!.w, at.critique!.x + at.critique!.w))
    expect(back[0]!.labelAt).not.toBeNull()
  })

  it("ranks a branching spec top to bottom without overlaps", () => {
    const l = layout(SPECS.triage, ["Webhook"])
    const at = Object.fromEntries(l.nodes.map((b) => [b.id, b]))
    expect(at[TRIGGERS]!.y).toBeLessThan(at[SPECS.triage.start]!.y)
    for (const e of l.edges.filter((x) => !x.back)) expect(at[e.to]!.y).toBeGreaterThanOrEqual(at[e.from]!.y + at[e.from]!.h)
    for (const a of l.nodes) for (const b of l.nodes) if (a !== b) expect(overlap(a, b), `${a.id} ${b.id}`).toBe(false)
    const fan = l.edges.filter((e) => e.from === SPECS.triage.start)
    expect(fan.length).toBeGreaterThan(1)
    expect(fan.every((e) => e.label)).toBe(true)
  })

  it("puts a + under steps with no next step, and none under Finish", () => {
    const s = structuredClone(SPECS.content)
    delete s.nodes.create_page!.next
    const l = layout(s)
    expect(l.leaves.map((x) => x.id)).toEqual(["create_page"])
  })
})

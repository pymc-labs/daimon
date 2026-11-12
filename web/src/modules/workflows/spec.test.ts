import { describe, expect, it } from "vitest"

import { SPECS } from "@/mock/workflows-specs"
import { insertNode, removeNode, validate } from "./spec"

const codes = (s: Parameters<typeof validate>[0]) => validate(s).errors.map((p) => `${p.code}:${p.node ?? ""}`)

describe("validate", () => {
  it("passes every seeded spec", () => {
    for (const [k, s] of Object.entries(SPECS)) expect(validate(s).errors, k).toEqual([])
  })

  it("finds unreachable steps, a missing Finish and unknown refs", () => {
    const s = structuredClone(SPECS.content)
    s.nodes.orphan = { kind: "agent", inputs: { x: "${{ steps.nope.output.x }}" }, next: "done" }
    s.nodes.write!.inputs = { ...s.nodes.write!.inputs, extra: "${{ inputs.missing }}" }
    expect(codes(s)).toEqual(expect.arrayContaining(["unreachable:orphan", "unknown_ref:orphan", "unknown_ref:write"]))
    for (const [id, n] of Object.entries(s.nodes)) if (n.kind === "finish") delete s.nodes[id]
    expect(codes(s)).toEqual(expect.arrayContaining(["no_finish:"]))
  })

  it("flags a loop without max visits and a dead end", () => {
    const s = structuredClone(SPECS.content)
    delete s.nodes.write!.max_visits
    expect(codes(s)).toContain("loop_limit:write")
    delete s.nodes.create_page!.next
    expect(codes(s)).toContain("dead_end:create_page")
  })

  it("keeps a spec valid when a step is inserted on an edge and removed again", () => {
    const t = insertNode(SPECS.content, { from: "approve", slot: "routes.approve", to: "create_page" }, "action")
    expect(t.spec.nodes.approve!.routes!.approve).toBe(t.id)
    expect(codes(removeNode(t.spec, t.id))).toEqual([])
  })
})

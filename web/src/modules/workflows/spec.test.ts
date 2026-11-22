import { describe, expect, it } from "vitest"

import { SPECS } from "@/mock/workflows-specs"
import type { Spec } from "./types"
import { insertNode, removeNode, safety, scopeText, setAnswer, validate } from "./spec"

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

  it("inserts the Ask a person preset and keeps its way out when the answer changes", () => {
    const t = insertNode(SPECS.content, { from: "approve", slot: "routes.approve", to: "create_page" }, "approval", "ask")
    const n = t.spec.nodes[t.id]!
    expect(n).toMatchObject({ answer: "text", next: "create_page" })
    setAnswer(n, "choice")
    expect(n).toMatchObject({ answer: "choice", routes: { option_1: "create_page", option_2: "create_page" }, next: undefined })
    setAnswer(n, "approve")
    expect(n).toMatchObject({ answer: undefined, routes: { approve: "create_page", reject: "create_page" } })
  })

  it("taints the answer of an approval that takes data from someone who is not an owner or editor", () => {
    const s: Spec = {
      schema: "daimon.workflow/v1", name: "W", visibility: "private", start: "review",
      nodes: {
        review: { kind: "approval", approvers: ["owner"], form: { note: { type: "string" } }, routes: { approve: "send", reject: "done" } },
        send: { kind: "action", tool: "gmail/send_message", args: { body: "${{ steps.review.output.note }}" }, next: "done" },
        done: { kind: "finish" },
      },
    }
    const review = s.nodes.review!
    expect(safety(s).classes.send).not.toContain("U")
    review.approvers = ["person:p_1"]
    expect(safety(s).classes.send).toContain("U")
  })
})

describe("loops and scoped grants", () => {
  const loop = (timeout?: { after: string; route: string }): Spec => ({
    schema: "daimon.workflow/v1", name: "loop", start: "work",
    nodes: {
      work: { kind: "agent", agent: "a", instructions: "Go", next: "ask" },
      ask: { kind: "approval", answer: "choice", routes: { again: "work", stop: "done" }, ...(timeout ? { timeout } : {}) },
      done: { kind: "finish" },
    },
  })

  it("counts an approval as a person gate unless its timeout route stays in the loop", () => {
    expect(codes(loop())).not.toContain("loop_limit:work")
    expect(codes(loop({ after: "1d", route: "stop" }))).not.toContain("loop_limit:work")
    expect(codes(loop({ after: "1d", route: "again" }))).toContain("loop_limit:work")
  })

  it("shows a scoped write's limits and free arguments in words", () => {
    expect(scopeText({ tool: "mail/send_reply", where: { to: { in: ["a@x.org"], domains: ["example.com"] } }, free: ["subject", "body"] })).toBe(
      "mail/send_reply: to is a@x.org or is an address at example.com. Any text in subject, body.",
    )
  })
})

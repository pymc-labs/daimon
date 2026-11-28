import { describe, expect, it } from "vitest"

import { SPECS } from "@/mock/workflows-specs"
import { badgesOf, CARD_H, CARD_H_BADGES, cardHeight } from "./cards"
import { layout } from "./layout"
import { validate } from "./spec"
import type { Spec } from "./types"

const kinds = (spec: Spec, id: string) => badgesOf(id, spec.nodes[id]!, validate(spec)).map((b) => b.kind)

describe("badgesOf", () => {
  it("shows the untrusted shield only where untrusted data reaches a write", () => {
    const s = SPECS.lead
    const v = validate(s)
    for (const [id, n] of Object.entries(s.nodes)) {
      const k = badgesOf(id, n, v).map((b) => b.kind)
      if (k.includes("untrusted")) expect(k, id).toContain("writes")
    }
    expect(kinds(s, "research")).not.toContain("untrusted")
    expect(kinds(s, "send")).toEqual(["writes", "untrusted"])
  })
})

describe("layout", () => {
  it("gives cards with badges their taller height", () => {
    const s = SPECS.lead
    const v = validate(s)
    const heights = Object.fromEntries(Object.entries(s.nodes).map(([id, n]) => [id, cardHeight(badgesOf(id, n, v))]))
    const at = Object.fromEntries(layout(s, [], heights).nodes.map((b) => [b.id, b]))
    expect(at.send!.h).toBe(CARD_H_BADGES)
    expect(at.research!.h).toBe(CARD_H)
  })
})

import { describe, expect, it } from "vitest"

import type { ModelPreview } from "./types"
import { NOTHING, unsupportedOf } from "./unsupported"

const base: ModelPreview = {
  moves: true, allowed: true, skills: ["pricing"], servers: ["mail"], capabilities: ["multiagent"], shared_limits: [], lost: [], drops: ["memory"],
  cost: "", threads: "", digest: "d", revision: 1, refusal: null,
}

describe("unsupportedOf", () => {
  it("lists the lost skills, servers and capabilities, drops included", () => {
    const u = unsupportedOf(base)
    expect(u.skills.has("pricing")).toBe(true)
    expect(u.servers.has("mail")).toBe(true)
    expect([...u.capabilities].sort()).toEqual(["memory", "multiagent"])
  })

  it("marks a skill lost by id from its drop", () => {
    expect(unsupportedOf({ ...base, drops: ["skill:sk_1"] }).skills.has("sk_1"), "the editor matches ids").toBe(true)
  })

  it("is empty with no preview, a pick that stays, or a refusal", () => {
    expect(unsupportedOf(undefined), "no preview").toBe(NOTHING)
    expect(unsupportedOf({ ...base, moves: false }), "stays on the provider").toBe(NOTHING)
    expect(unsupportedOf({ ...base, refusal: { line: "No", action: null } }), "refused").toBe(NOTHING)
  })
})

import { describe, expect, it } from "vitest"

import { pageTitle } from "@/lib/title"
import type { Capability, Me } from "@/lib/types"
import { redirectFor, sidebarFor } from "./registry"

const MEMBER: Capability[] = ["chat.use", "agents.view", "agents.edit", "agents.create", "routines.view", "routines.manage", "credits.view_own"]
const ADMIN: Capability[] = [...MEMBER, "credits.manage", "integrations.view", "integrations.manage", "people.manage", "audit.view", "workspace.manage"]

const person = (over: Partial<Me> = {}): Me => ({
  id: "p1", name: "Ada", email: "ada@example.com", role: "member",
  workspace: { id: "w1", name: "Daimon QA", platform: "discord", role: "member", members: 3 }, workspaces: [],
  modules: ["chat", "agents", "routines", "credits", "integrations", "admin"], capabilities: MEMBER, identities: [], accounts: [], credit: null, ...over,
})

describe("sidebar", () => {
  it("hides credits for a member with no allowance and admin without admin rights", () => {
    expect(sidebarFor(person()).map((i) => i.to)).toEqual(["/chat", "/agents", "/routines"])
  })

  it("shows credits once an allowance governs the person", () => {
    const credit = { left_usd: 4, total_usd: 10, window: "month" as const, resets_at: null }
    expect(sidebarFor(person({ credit })).map((i) => i.to)).toEqual(["/chat", "/agents", "/routines", "/credits"])
  })

  it("shows credits and admin to an admin", () => {
    expect(sidebarFor(person({ capabilities: ADMIN, role: "admin" })).map((i) => i.to)).toEqual(["/chat", "/agents", "/routines", "/credits", "/integrations", "/admin"])
  })
})

describe("redirects", () => {
  it("sends a hidden page to the first page the person may use", () => {
    const me = person()
    expect(redirectFor(me, "/credits")).toBe("/chat")
    expect(redirectFor(me, "/admin/people")).toBe("/chat")
    expect(redirectFor(me, "/integrations/tokens")).toBe("/chat")
    expect(redirectFor(me, "/agents/new")).toBeNull()
    expect(redirectFor(person({ capabilities: ["chat.use"] }), "/agents/new")).toBe("/chat")
    expect(redirectFor(person({ capabilities: ["chat.use"] }), "/routines")).toBe("/chat")
    expect(redirectFor(me, "/routines")).toBeNull()
  })
})

describe("page title", () => {
  it("lists the most specific part first", () => {
    expect(pageTitle(["Agent access", "Admin"], "Daimon QA")).toBe("Agent access · Admin · Daimon QA")
  })

  it("skips empty and repeated parts", () => {
    expect(pageTitle(["", "Almanac", "Almanac"], "Daimon QA")).toBe("Almanac · Daimon QA")
    expect(pageTitle([], undefined)).toBe("Daimon")
  })
})

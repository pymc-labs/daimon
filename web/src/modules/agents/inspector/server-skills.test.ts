import { describe, expect, it } from "vitest"

import type { ServerSkillOption, Skill } from "../types"
import { menuEntries, skillBadge } from "./server-skills"

const option = (name: string, extra: Partial<ServerSkillOption> = {}): ServerSkillOption =>
  ({ uri: `skill://${name}`, name, description: "", skill_id: null, taken: false, ...extra })
const skill = (id: string, title = id): Skill => ({ type: "custom", skill_id: id, title, description: "", version: "1" })

describe("menuEntries", () => {
  it("hides skills the agent has and offers the rest", () => {
    const got = menuEntries([option("plots", { skill_id: "sk_1" }), option("notes")], [skill("sk_1")])
    expect(got).toEqual([{ option: option("notes"), disabled: null }])
  })

  it("disables a name another skill holds", () => {
    const got = menuEntries([option("notes", { taken: true }), option("data-check")], [skill("sk_2", "Data check")])
    expect(got.map((e) => e.disabled)).toEqual(["Name taken", "Name taken"])
  })

  it("disables everything at the cap", () => {
    const full = Array.from({ length: 20 }, (_, i) => skill(`sk_${i}`))
    expect(menuEntries([option("notes")], full)[0]!.disabled).toBe("20 skills max")
  })
})

describe("skillBadge", () => {
  it("puts trouble first", () => {
    expect(skillBadge({ follow: true, status: "unavailable", version: "3" }, "3").label).toBe("Unavailable")
    expect(skillBadge({ follow: true, status: "pending", version: "3" }, "3").label).toBe("Needs approval")
  })

  it("names following, pinned and an update", () => {
    expect(skillBadge({ follow: true, status: "current", version: "3" }, "3").label).toBe("Following")
    expect(skillBadge({ follow: false, status: "current", version: "3" }, "3").label).toBe("Pinned v3")
    expect(skillBadge({ follow: false, status: "current", version: "4" }, "3")).toEqual({ label: "Update available", tone: "warn", update: "4" })
  })
})

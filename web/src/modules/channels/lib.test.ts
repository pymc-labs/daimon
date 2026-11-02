import { describe, expect, it } from "vitest"

import { accessAsk, budgetStops, describeChange, type Filters, filterChannels, groupChannels, nameOf, sourcedLabel, spentFor } from "./lib"
import type { Channel } from "./types"

const channel = (id: string, name: string, more: Partial<Channel> = {}): Channel => ({
  id, platform: "discord", name, kind: "text", parent: null, last_seen_at: null, agent: { value: "daimon", source: "workspace" },
  environment: { value: null, source: null }, readers: "any", writers: "any", kept_agents: [], tags: [], budget: null,
  spend: { month_usd: 0, last_30_days_usd: 0 }, last_active_at: null, ...more,
})
const cat = (name: string) => ({ id: `k_${name}`, name })
const none: Filters = { q: "", platforms: [], access: [], budget: [], agents: [] }

describe("groupChannels", () => {
  it("puts web chat and loose channels first, then groups by name with rows sorted", () => {
    const rows = [
      channel("b", "beta", { parent: cat("Projects") }), channel("w", "Web chat", { platform: "web" }),
      channel("a", "alpha", { parent: cat("Projects") }), channel("g", "general"), channel("l", "board", { parent: cat("Leadership") }),
    ]
    const g = groupChannels(rows)
    expect(g.map((x) => x.name)).toEqual([null, "Leadership", "Projects"])
    expect(g[0]!.rows.map((c) => c.id)).toEqual(["w", "g"])
    expect(g[2]!.rows.map((c) => c.id)).toEqual(["a", "b"])
  })

  it("keeps a category and a team of the same id apart", () => {
    const g = groupChannels([channel("a", "a", { parent: cat("X") }), channel("b", "b", { platform: "teams", parent: cat("X") })])
    expect(g).toHaveLength(2)
  })
})

describe("filterChannels", () => {
  const rows = [
    channel("1", "clv-models", { parent: cat("Projects"), budget: { limit_usd: "25.00", window: "monthly", starts_at: null, ends_at: null, spent_usd: 1, remaining_usd: 24, percent_used: 4, active: true, exceeded: false }, agent: { value: "clv", source: "channel" } }),
    channel("2", "board", { tags: ["locked"], platform: "slack" }),
    channel("3", "general"),
  ]
  const ids = (f: Partial<Filters>) => filterChannels(rows, { ...none, ...f }).map((c) => c.id)

  it("searches name and parent", () => {
    expect(ids({ q: "CLV" })).toEqual(["1"])
    expect(ids({ q: "projects" })).toEqual(["1"])
  })
  it("ORs within a facet and ANDs across", () => {
    expect(ids({ platforms: ["slack", "discord"] })).toEqual(["1", "2", "3"])
    expect(ids({ platforms: ["discord"], access: ["open"] })).toEqual(["1", "3"])
    expect(ids({ access: ["locked", "open"], budget: ["no"] })).toEqual(["2", "3"])
  })
  it("filters by budget and agent", () => {
    expect(ids({ budget: ["yes"] })).toEqual(["1"])
    expect(ids({ agents: ["clv"] })).toEqual(["1"])
    expect(ids({ agents: ["daimon"] })).toEqual(["2", "3"])
  })
})

describe("accessAsk", () => {
  type Rule = Parameters<typeof accessAsk>[0]
  const open: Rule = { readers: "any", writers: "any", agent: { value: "daimon", source: "workspace" }, kept_agents: [] }
  it("asks before only its own agents and names the copy", () => {
    expect(accessAsk(open, { readers: "own" })?.body).toBe("A copy of daimon is made for this channel.")
    expect(accessAsk(open, { writers: "own" })?.title).toBe("Only its own agents?")
    const kept: Rule = { ...open, agent: { value: "desk", source: "channel" }, kept_agents: ["desk"] }
    expect(accessAsk(kept, { readers: "own" })?.body).toBe("desk stays here.")
  })
  it("asks before leaving own agents", () => {
    const own: Rule = { ...open, readers: "own", kept_agents: ["desk"] }
    expect(accessAsk(own, { readers: "any" })).toMatchObject({ title: "Leave its own agents?", body: "desk may run anywhere again." })
    expect(accessAsk(own, { writers: "any" })).toBeNull()
  })
  it("asks before nobody can post, once", () => {
    expect(accessAsk(open, { writers: "none" })?.destructive).toBe(true)
    expect(accessAsk({ ...open, writers: "none" }, { readers: "inside" })).toBeNull()
  })
  it("applies other changes at once", () => {
    expect(accessAsk(open, { readers: "inside" })).toBeNull()
    expect(accessAsk({ ...open, readers: "own" }, { readers: "own" })).toBeNull()
  })
})

describe("budget stops", () => {
  const withBudget = (spent: number, window: "monthly" | "total") => ({
    budget: { limit_usd: "10.00", window, starts_at: null, ends_at: null, spent_usd: spent, remaining_usd: 0, percent_used: 0, active: true, exceeded: false },
    spend: { month_usd: 4, last_30_days_usd: 9 },
  })
  it("compares with the budget's spend while its window holds, else this month's", () => {
    expect(spentFor(withBudget(7, "monthly"), "monthly")).toBe(7)
    expect(spentFor(withBudget(7, "total"), "monthly")).toBe(4)
    expect(spentFor({ budget: null, spend: { month_usd: 4, last_30_days_usd: 9 } }, "total")).toBe(0)
  })
  it("stops at or below spend", () => {
    expect(budgetStops(5, 5)).toBe(true)
    expect(budgetStops(5.01, 5)).toBe(false)
    expect(budgetStops(0, 0)).toBe(false)
  })
})

describe("labels", () => {
  it("shows where a default comes from", () => {
    expect(sourcedLabel({ value: "daimon", source: "workspace" })).toBe("Workspace default: daimon")
    expect(sourcedLabel({ value: "clv", source: "channel" })).toBe("clv")
    expect(sourcedLabel({ value: null, source: null })).toBe("None")
  })
  it("names web chat plainly and channels with a hash", () => {
    expect(nameOf({ platform: "web", name: "Web chat" })).toBe("Web chat")
    expect(nameOf({ platform: "discord", name: "general" })).toBe("#general")
    expect(nameOf({ platform: "discord", name: null })).toBe("Unnamed channel")
  })
})

describe("describeChange", () => {
  const labels = { readers: [{ value: "any" as const, label: "Anyone" }, { value: "own" as const, label: "Only its own agents" }], writers: [{ value: "none" as const, label: "Nobody" }, { value: "any" as const, label: "Anyone" }] }
  const entry = (setting: Parameters<typeof describeChange>[0]["setting"], before: unknown, after: unknown) => ({ at: "", setting, before, after, actor: null, surface: null })
  it("uses the server's labels for access", () => {
    expect(describeChange(entry("access", { readers: "any", writers: "any" }, { readers: "own", writers: "any" }), labels)).toEqual({ what: "Who can read", from: "Anyone", to: "Only its own agents" })
    expect(describeChange(entry("access", { readers: "any", writers: "any" }, { readers: "any", writers: "none" }), labels)).toEqual({ what: "Who can post", from: "Anyone", to: "Nobody" })
  })
  it("reads kept agents, budgets and agents", () => {
    expect(describeChange(entry("kept_agents", ["a"], ["a", "b"]), labels)).toEqual({ what: "Kept agents", from: "", to: "added b" })
    expect(describeChange(entry("budget", null, { limit_usd: "25.00", window: "monthly" }), labels)).toEqual({ what: "Budget", from: "none", to: "$25.00 monthly" })
    expect(describeChange(entry("default_agent", null, "clv"), labels)).toEqual({ what: "Default agent", from: "none", to: "clv" })
  })
})

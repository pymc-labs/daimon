import { describe, expect, it } from "vitest"

import { flatten } from "@/components/tree-state"

import {
  accessAsk, accessFlags, agentChoices, agentsOf, budgetStops, channelTree, choiceOf, cronWords, defaultSource, describeChange, fillDays,
  type Filters, filterChannels, nameOf, overrideCount, spendLines, spentFor, switchChange, threadValue, toolRuleChange, treeFilter, unavailableText,
} from "./lib"
import type { Channel } from "./types"

const channel = (id: string, name: string, more: Partial<Channel> = {}): Channel => ({
  id, platform: "discord", name, kind: "channel", status: "active", parent: null, path: [], last_seen_at: null, agent: { value: "daimon", source: "workspace" },
  environment: { value: null, source: null }, readers: "any", writers: "any", kept_agents: [], tags: [], budget: null,
  spend: { month_usd: 0, last_30_days_usd: 0 }, last_active_at: null, ...more,
})
const category = (name: string, platform = "discord") => ({ id: `k_${name}`, name, kind: platform === "teams" ? "team" as const : "category" as const })
const inCategory = (name: string, platform = "discord") => ({ parent: { id: `k_${name}`, name }, path: [category(name, platform)] })
const none: Filters = { q: "", platforms: [], access: [], budget: [], agents: [], override: null }

describe("channelTree", () => {
  const ids = (rows: Channel[]) => channelTree(rows).map((n) => [n.id, n.parentId])

  it("nests a channel under its category and a thread under its channel", () => {
    const rows = [
      channel("g", "general", inCategory("Projects")),
      channel("t", "ideas", { kind: "thread", path: [category("Projects"), { id: "g", name: "general", kind: "channel" }] }),
    ]
    expect(ids(rows)).toEqual([["g", "g:discord:category:k_Projects"], ["t", "g"], ["g:discord:category:k_Projects", null]])
  })

  it("puts web chat first, then channels before categories, each by name", () => {
    const rows = [
      channel("b", "beta", inCategory("Projects")), channel("w", "Web chat", { platform: "web" }),
      channel("z", "zed"), channel("a", "alpha"), channel("l", "board", inCategory("Leadership")),
    ]
    const top = channelTree(rows).filter((n) => n.parentId === null).map((n) => n.id)
    expect(top).toEqual(["w", "a", "z", "g:discord:category:k_Leadership", "g:discord:category:k_Projects"])
  })

  it("keeps a category and a team of the same id apart and orders roots by platform", () => {
    const rows = [channel("a", "a", { platform: "teams", ...inCategory("X", "teams") }), channel("b", "b", inCategory("X"))]
    const groups = channelTree(rows).filter((n) => n.kind === "group")
    expect(groups.map((g) => g.id)).toEqual(["g:discord:category:k_X", "g:teams:team:k_X"])
  })

  it("lets Slack channels stand at the root with threads below", () => {
    const rows = [channel("s", "ops", { platform: "slack" }), channel("t", "thread", { platform: "slack", kind: "thread", path: [{ id: "s", name: "ops", kind: "channel" }] })]
    expect(ids(rows)).toEqual([["s", null], ["t", "s"]])
  })

  it("holds a thread whose channel is not listed under a group for that channel", () => {
    const rows = [channel("t", "ideas", { kind: "thread", path: [category("Projects"), { id: "gone", name: "lost", kind: "channel" }] })]
    const tree = channelTree(rows)
    const group = tree.find((n) => n.id === "g:discord:channel:gone")
    expect(group).toMatchObject({ kind: "group", parentId: "g:discord:category:k_Projects", row: { type: "group", name: "lost" } })
    expect(tree.find((n) => n.id === "t")?.parentId).toBe("g:discord:channel:gone")
  })

  it("keeps a team and its General channel, which share an id, as two groups", () => {
    const rows = [channel("19:team;messageid=7", "reply", { platform: "teams", kind: "thread", path: [{ id: "19:team", name: "Platform", kind: "team" }, { id: "19:team", name: null, kind: "channel" }] })]
    expect(ids(rows)).toEqual([["19:team;messageid=7", "g:teams:channel:19:team"], ["g:teams:channel:19:team", "g:teams:team:19:team"], ["g:teams:team:19:team", null]])
  })

  it("opens ancestors of a match and a group that matches a plain search keeps its rows", () => {
    const rows = [channel("g", "general", inCategory("Projects")), channel("o", "other", inCategory("Leadership"))]
    const tree = channelTree(rows)
    const seen = (f: Partial<Filters>) => flatten(tree, {}, treeFilter({ ...none, ...f })).map((x) => x.node.id)
    expect(seen({ q: "gener" })).toEqual(["g:discord:category:k_Projects", "g"])
    expect(seen({ q: "leader" })).toEqual(["g:discord:category:k_Leadership", "o"])
    expect(treeFilter(none)).toBeNull()
  })
})

describe("agents", () => {
  const refs = { daimon: { id: "a1", name: "Daimon" }, clv: { id: "a2", name: "Customer value" }, old: { id: null, name: null } }
  it("lists the keys in use by name, sorted", () => {
    const rows = [channel("1", "a", { agent: { value: "daimon", source: "workspace" } }), channel("2", "b", { agent: { value: "clv", source: "channel" } }), channel("3", "c", { agent: { value: "clv", source: "channel" } })]
    expect(agentsOf(rows, refs)).toEqual([{ key: "clv", name: "Customer value" }, { key: "daimon", name: "Daimon" }])
  })
  it("keeps a key already in use for the agent it names and shows a deleted one", () => {
    const roster = [{ name: "Customer value", avatar: null }, { name: "Daimon", avatar: null }]
    const choices = agentChoices(roster, ["clv", "old"], refs)
    expect(choices.map((c) => [c.key, c.name])).toEqual([["old", "Deleted agent"], ["clv", "Customer value"], ["Daimon", "Daimon"]])
  })
  it("never lists two choices under one key", () => {
    const roster = [{ name: "Daimon", avatar: null }, { name: "Daimon", avatar: "x.png" }, { name: "Daimon Reader", avatar: null }]
    const keys = agentChoices(roster, ["daimon", "daimon"], { daimon: { id: "a1", name: "Daimon Reader" } }).map((c) => c.key)
    expect(new Set(keys).size).toBe(keys.length)
  })
})

describe("filterChannels", () => {
  const rows = [
    channel("1", "clv-models", { ...inCategory("Projects"), budget: { limit_usd: "25.00", window: "monthly", starts_at: null, ends_at: null, spent_usd: 1, remaining_usd: 24, percent_used: 4, active: true, exceeded: false }, agent: { value: "clv", source: "channel" } }),
    channel("2", "board", { tags: ["locked"], platform: "slack" }),
    channel("3", "general"),
  ]
  const ids = (f: Partial<Filters>) => filterChannels(rows, { ...none, ...f }).map((c) => c.id)

  it("searches the name only", () => {
    expect(ids({ q: "CLV" })).toEqual(["1"])
    expect(ids({ q: "projects" })).toEqual([])
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
  it("names web chat plainly and channels with a hash", () => {
    expect(nameOf({ platform: "web", name: "Web chat" })).toBe("Web chat")
    expect(nameOf({ platform: "discord", name: "general" })).toBe("#general")
    expect(nameOf({ platform: "discord", name: null })).toBe("Unnamed channel")
  })
})

describe("overrides", () => {
  it("counts the channels that set a value themselves", () => {
    const rows = [channel("1", "a", { agent: { value: "x", source: "channel" } }), channel("2", "b"), channel("3", "c", { environment: { value: "e", source: "channel" } })]
    expect(overrideCount(rows, "agent")).toBe(1)
    expect(overrideCount(rows, "sandbox")).toBe(1)
    expect(filterChannels(rows, { ...none, override: "agent" }).map((c) => c.id)).toEqual(["1"])
  })
})

describe("describeChange", () => {
  const labels = { readers: [{ value: "any" as const, label: "Anyone" }, { value: "own" as const, label: "Only its own agents" }], writers: [{ value: "none" as const, label: "Nobody" }, { value: "any" as const, label: "Anyone" }] }
  const entry = (setting: Parameters<typeof describeChange>[0]["setting"], before: unknown, after: unknown) => ({ at: "", setting, before, after, actor: null, surface: null })
  it("uses the server's labels for access", () => {
    expect(describeChange(entry("access", { readers: "any", writers: "any" }, { readers: "own", writers: "any" }), labels)).toEqual({ what: "Who can read", from: ["Anyone"], to: ["Only its own agents"] })
    expect(describeChange(entry("access", { readers: "any", writers: "any" }, { readers: "any", writers: "none" }), labels)).toEqual({ what: "Who can post", from: ["Anyone"], to: ["Nobody"] })
  })
  it("keeps agent keys as keys so they can be linked", () => {
    expect(describeChange(entry("kept_agents", ["a"], ["a", "b"]), labels)).toEqual({ what: "Kept here", from: [], to: ["added ", { agent: "b" }] })
    expect(describeChange(entry("default_agent", null, "clv"), labels)).toEqual({ what: "Default agent", from: ["None"], to: [{ agent: "clv" }] })
  })
  it("reads budgets", () => {
    expect(describeChange(entry("budget", null, { limit_usd: "25.00", window: "monthly" }), labels)).toEqual({ what: "Budget", from: ["None"], to: ["$25.00 monthly"] })
  })
})

describe("leaving its own agents", () => {
  const own = { readers: "own" as const, writers: "own" as const, agent: { value: "ops", source: "channel" as const }, kept_agents: ["ops"] }

  it("offers to keep their rules next to releasing them", () => {
    const ask = accessAsk(own, { readers: "inside" })
    expect(ask).toMatchObject({ action: "Release agents", alternate: "Keep their rules" })
  })

  it("has nothing to keep when no agent is kept here", () => {
    expect(accessAsk({ ...own, kept_agents: [] }, { readers: "any" })?.alternate).toBeUndefined()
  })

  it("sends exactly one of release_agents and keep_rules", () => {
    expect(accessFlags(own, { readers: "any" }, "action")).toEqual({ release_agents: true })
    expect(accessFlags(own, { readers: "any" }, "alternate")).toEqual({ keep_rules: true })
  })

  it("sends neither when the readers do not leave own, and copies the agent when they enter it", () => {
    expect(accessFlags({ ...own, readers: "any" }, { writers: "none" })).toEqual({})
    expect(accessFlags({ ...own, readers: "any" }, { readers: "own" })).toEqual({ copy_agent: true })
  })
})

describe("switches", () => {
  it("reads no own value as inherit and sends null to clear it", () => {
    expect(choiceOf({ own: null })).toBe("inherit")
    expect(switchChange("inherit", { own: "on" })).toEqual({ mode: null, expected: "on" })
    expect(switchChange("off", { own: null })).toEqual({ mode: "off", expected: null })
  })

  it("says participation is not on Slack and keeps value and source apart for defaults", () => {
    expect(unavailableText("participation", "slack")).toBe("Not on Slack")
    expect(unavailableText("progress_plans", "slack")).toBe("Not available")
    expect(defaultSource({ source: "workspace" })).toBe("Workspace")
    expect(defaultSource({ source: "deployment" })).toBe("Built-in default")
    expect(defaultSource({ source: null })).toBeNull()
  })
})

describe("spend", () => {
  const now = Date.UTC(2026, 10, 3, 15)

  it("fills 30 days ending today with zeros for days without spend", () => {
    const days = fillDays([{ key: "2026-11-01", name: null, usd: 4 }, { key: "2026-10-05", name: null, usd: 1 }], 30, now)
    expect(days).toHaveLength(30)
    expect(days[29]).toEqual({ day: "2026-11-03", usd: 0 })
    expect(days[27]).toEqual({ day: "2026-11-01", usd: 4 })
    expect(days[0]!.day).toBe("2026-10-05")
    expect(days[0]!.usd).toBe(1)
  })

  it("drops spend older than the window", () => {
    expect(fillDays([{ key: "2026-09-01", name: null, usd: 9 }], 30, now).every((d) => d.usd === 0)).toBe(true)
  })

  it("lists biggest first with shares, naming spend with no id", () => {
    const lines = spendLines([{ key: null, name: null, usd: 1 }, { key: "a", name: "clv", usd: 3 }, { key: "b", name: null, usd: 0 }], "No person")
    expect(lines.map((l) => [l.label, l.share])).toEqual([["clv", 0.75], ["No person", 0.25], ["Unknown", 0]])
  })
})

describe("channel rows", () => {
  it("writes a schedule in words and keeps shapes it cannot draw", () => {
    expect(cronWords("0 9 * * 1")).toBe("Mondays at 09:00")
    expect(cronWords("30 8 * * 1-5")).toBe("Weekdays at 08:30")
    expect(cronWords("5 4 * * 1,3")).toBe("5 4 * * 1,3")
  })

  it("labels a thread's own value by its kind", () => {
    const labels = { readers: [{ value: "own" as const, label: "Only its own agents" }], writers: [{ value: "none" as const, label: "Nobody" }] }
    expect(threadValue("participation", "off", labels)).toBe("Off")
    expect(threadValue("thread_rule", "own/none", labels)).toBe("Only its own agents, Nobody")
    expect(threadValue("handoff", "clv", labels)).toBe("clv")
  })

  it("compares a tool rule with the revision of the same agent and tool", () => {
    const rules = [{ agent: "a", tool: "x", tool_label: "X", server_name: "S", rule: "ask" as const, scope: "chat_and_routines" as const, revision: 4 }]
    expect(toolRuleChange({ agent: "a", tool: "x", rule: "deny" }, rules)).toMatchObject({ scope: "chat_and_routines", expected_revision: 4 })
    expect(toolRuleChange({ agent: "a", tool: "y", rule: "allow" }, rules).expected_revision).toBe(0)
  })
})

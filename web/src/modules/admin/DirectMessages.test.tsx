import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import type { FeatureView } from "@/modules/features/types"
import { AgentDm, DmSettingsView } from "./DirectMessages"
import type { DmExplain, DmGrant, DmSettings } from "./types"

// The select's pointer handling needs what jsdom leaves out.
Object.assign(Element.prototype, { hasPointerCapture: () => false, releasePointerCapture: () => {}, scrollIntoView: () => {} })

const ok = <T,>(data: T) => ({ data, error: null, isError: false, refetch: () => {} })
const run = vi.fn(async () => null)
const writes = { pending: false, setDefault: vi.fn(), add: vi.fn(), change: vi.fn(), remove: vi.fn() }
const explain = { mutate: vi.fn(), reset: vi.fn(), isPending: false, data: undefined as DmExplain | undefined }
let feature: FeatureView
vi.mock("@/modules/features/api", () => ({
  useFeatures: () => ok({ platform: "discord", features: [feature] }),
  useFeatureSave: () => ({ pending: false, run }),
}))
vi.mock("@/lib/hooks", () => ({ useChatAgents: () => ok([]) }))
vi.mock("./dm", () => ({
  usePlatformGroups: () => ok({ platform: "discord", groups: [{ id: "r1", name: "Staff" }], problem: null }),
  useDmPeople: () => ok([]),
  useDmSave: () => writes,
  useDmExplain: () => explain,
}))

const agent = { id: "ag_1", name: "Ada", avatar: "", model: "Sonnet", description: "" }
const view = (over: Partial<FeatureView> = {}): FeatureView => ({
  key: "direct_messages", tiers: [{ tier: "workspace", value: "default", locked: false, text: "default" }, { tier: "agent", value: "default", locked: false, text: "default" }],
  value: "on", source: "default", locked: false, unavailable: null, summary: "On", ...over,
})
const listed: DmGrant = { on: true, who: "listed", users: [{ id: "u1", name: "Bea" }], groups: ["r1"] }
const settings: DmSettings = {
  platform: "discord", default_agent: "Ada", agents: [{ id: "ag_1", name: "Ada" }],
  rules: [
    { position: 0, subject: "group", subject_id: "r1", name: null, agent: "Ada", locked: true },
    { position: 1, subject: "user", subject_id: "u1", name: "Bea", agent: null, locked: false },
  ],
}
const wrap = (ui: React.ReactNode) => render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>)

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  feature = view()
  explain.data = undefined
})

describe("AgentDm", () => {
  it("keeps the lists when another who card is picked", async () => {
    const save = vi.fn()
    wrap(<AgentDm agent={agent} dm={listed} save={save} />)
    expect(screen.getByRole("radio", { name: "Only listed" }), "the stored choice is checked").toHaveAttribute("aria-checked", "true")
    await userEvent.click(screen.getByRole("radio", { name: "Everyone except listed" }))
    expect(save, "the people and groups carry over").toHaveBeenCalledWith({ ...listed, who: "excluded" })
  })

  it("saves the rest when a chip is removed", async () => {
    const save = vi.fn()
    wrap(<AgentDm agent={agent} dm={listed} save={save} />)
    expect(screen.getByText("Staff"), "a group shows its live name").toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Remove Bea" }))
    expect(save, "only the removed person goes").toHaveBeenCalledWith({ ...listed, users: [] })
  })

  it("writes the agent tier when the switch flips", async () => {
    wrap(<AgentDm agent={agent} dm={listed} save={vi.fn()} />)
    await userEvent.click(screen.getByRole("switch", { name: "Direct messages for Ada" }))
    expect(run, "the agent's own value changes, unlocked").toHaveBeenCalledWith({ key: "direct_messages", tier: "agent", value: "off", locked: false, expected: "default" })
  })

  it("shows the lock instead of the switch when pinned above", () => {
    feature = view({ value: "off", source: "workspace", locked: true })
    wrap(<AgentDm agent={agent} dm={{ ...listed, on: false }} save={vi.fn()} />)
    expect(screen.queryByRole("switch", { name: "Direct messages for Ada" }), "no switch under a lock").toBeNull()
    expect(screen.getByText("Locked by the workspace"), "the lock names its level").toBeInTheDocument()
  })

  it("hides the pickers for everyone", () => {
    wrap(<AgentDm agent={agent} dm={{ ...listed, who: "members", users: [], groups: [] }} save={vi.fn()} />)
    expect(screen.queryByText("People"), "everyone needs no list").toBeNull()
  })
})

describe("DmSettingsView", () => {
  it("lists rules in order with names and locks", () => {
    wrap(<DmSettingsView s={settings} />)
    const rows = screen.getAllByRole("row").slice(1)
    expect(rows.map((r) => within(r).getAllByRole("cell")[1]!.textContent), "a group by its live name, a person by stored name").toEqual(["Staff", "Bea"])
    expect(screen.getByRole("switch", { name: "Lock rule 1" }), "a locked rule shows it").toBeChecked()
    expect(screen.getByRole("switch", { name: "Lock rule 2" }), "a rule to the default agent cannot lock").toBeDisabled()
    expect(screen.getByRole("button", { name: "Move rule 1 up" }), "the first rule cannot go up").toBeDisabled()
  })

  it("sends the position when a rule moves or goes", async () => {
    wrap(<DmSettingsView s={settings} />)
    await userEvent.click(screen.getByRole("button", { name: "Move rule 1 down" }))
    await userEvent.click(screen.getByRole("button", { name: "Remove rule 2" }))
    expect(writes.change, "a move names the new position").toHaveBeenCalledWith(0, { to: 1 })
    expect(writes.remove, "a removal names the rule").toHaveBeenCalledWith(1)
  })

  it("adds a rule for everyone without an id", async () => {
    wrap(<DmSettingsView s={settings} />)
    expect(screen.getByRole("button", { name: "Add rule" }), "a group rule needs a group first").toBeDisabled()
    await userEvent.click(screen.getByRole("combobox", { name: "Rule for" }))
    await userEvent.click(await screen.findByRole("option", { name: "Everyone" }))
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }))
    expect(writes.add, "everyone, to the default agent").toHaveBeenCalledWith({ subject: "everyone", subject_id: null, agent: null, locked: false })
  })

  it("checks a pasted id", async () => {
    wrap(<DmSettingsView s={settings} />)
    const check = screen.getByRole("button", { name: "Check" })
    expect(check, "a person comes first").toBeDisabled()
    await userEvent.click(screen.getAllByRole("button", { name: "Choose" }).at(-1)!)
    await userEvent.type(screen.getByPlaceholderText("Find a person or paste an id"), "12345")
    await userEvent.click(await screen.findByRole("option", { name: "12345" }))
    await userEvent.keyboard("{Escape}")
    await userEvent.click(check)
    expect(explain.mutate, "only the person: their groups are read from the platform").toHaveBeenCalledWith({ user: "12345" })
  })

  it("shows the route and the rule that matched", () => {
    explain.data = { route: "start", agent: "Ada", reason: "locked", agents: [], allowed: ["Ada"], rule: 0, groups: ["r1"] }
    wrap(<DmSettingsView s={settings} />)
    expect(screen.getByText("Rule 1, locked"), "the rule that matched, by number").toBeInTheDocument()
    expect(screen.getByText("Goes to").nextSibling?.textContent, "the agent it starts with").toBe("Ada")
    expect(screen.getByText("Groups").nextSibling?.textContent, "the groups read, by name").toBe("Staff")
  })

  it("says when their groups could not be read", () => {
    explain.data = { route: "unchecked", agent: null, reason: null, agents: [], allowed: [], rule: null, groups: null }
    wrap(<DmSettingsView s={settings} />)
    expect(screen.getByText("Groups").nextSibling?.textContent, "unknown, not empty").toBe("Unknown")
  })
})

import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { FeatureSettings } from "./Settings"
import type { FeatureSchema, FeatureView } from "./types"

// The select's pointer handling needs what jsdom leaves out.
Object.assign(Element.prototype, { hasPointerCapture: () => false, releasePointerCapture: () => {}, scrollIntoView: () => {} })

const run = vi.fn()
const ok = <T,>(data: T) => ({ data, error: null, isError: false, refetch: () => {} })
const schema = (key: string, over: Partial<FeatureSchema> = {}): FeatureSchema => ({
  key, name: key, category: "chat", categoryName: "Chat", default: "on", tiers: ["deployment", "workspace", "agent", "channel", "thread"],
  lockable: ["workspace", "channel"], platforms: ["discord"], threadAnyone: false, native: false, doc: "",
  values: [{ value: "on", label: "On" }, { value: "off", label: "Off" }], ...over,
})
const view = (key: string, over: Partial<FeatureView> = {}): FeatureView => ({
  key, tiers: [{ tier: "workspace", value: "default", locked: false, text: "default" }, { tier: "channel", value: "default", locked: false, text: "default" }],
  value: "on", source: "default", locked: false, unavailable: null, summary: "On", ...over,
})
let features: FeatureView[] = []
let schemas: FeatureSchema[] = []
vi.mock("./api", () => ({
  useFeatureSchema: () => ok(schemas),
  useFeatures: () => ok({ platform: "discord", features }),
  useFeatureSave: () => ({ pending: false, run }),
}))

describe("FeatureSettings", () => {
  beforeEach(() => {
    run.mockReset()
    schemas = [schema("progress_plans", { name: "Progress plans" }), schema("guidance", { name: "Setup guidance", tiers: ["deployment", "workspace", "agent"] })]
    features = [view("progress_plans"), view("guidance")]
  })

  it("draws a control for each key set at this level, from the schema", () => {
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    expect(screen.getByRole("combobox", { name: "Progress plans" })).toHaveTextContent("Built-in default (On)")
    expect(screen.queryByRole("combobox", { name: "Setup guidance" })).not.toBeInTheDocument()
  })

  it("draws a key it has never heard of, with no code for it", () => {
    schemas = [...schemas, schema("brand_new", { name: "Brand new" })]
    features = [...features, view("brand_new")]
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    expect(screen.getByRole("combobox", { name: "Brand new" })).toBeInTheDocument()
  })

  it("sends a new value with the value it read", async () => {
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    await userEvent.click(screen.getByRole("combobox", { name: "Progress plans" }))
    await userEvent.click(await screen.findByRole("option", { name: "Off" }))
    expect(run).toHaveBeenCalledWith({ key: "progress_plans", tier: "channel", value: "off", locked: false, expected: "default" })
  })

  it("clears a value by choosing the default", async () => {
    features = [view("progress_plans", { value: "off", source: "channel", tiers: [{ tier: "channel", value: "off", locked: false, text: "off" }] })]
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    await userEvent.click(screen.getByRole("combobox", { name: "Progress plans" }))
    await userEvent.click(await screen.findByRole("option", { name: /default/ }))
    expect(run).toHaveBeenCalledWith(expect.objectContaining({ key: "progress_plans", value: "default", expected: "off" }))
  })

  it("locks a value set here", async () => {
    features = [view("progress_plans", { value: "off", source: "channel", tiers: [{ tier: "channel", value: "off", locked: false, text: "off" }] })]
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    await userEvent.click(screen.getByRole("button", { name: "Lock Progress plans" }))
    expect(run).toHaveBeenCalledWith(expect.objectContaining({ value: "off", locked: true, expected: "off" }))
  })

  it("shows the value in effect, and no controls, to a person who may not change it", () => {
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit={false} />)
    expect(screen.queryAllByRole("combobox")).toHaveLength(0)
    expect(screen.getAllByText("On").length).toBeGreaterThan(0)
  })

  it("says what is in effect when another level overrides the value set here", () => {
    features = [view("progress_plans", {
      value: "off", source: "workspace", summary: "Off, set for the workspace",
      tiers: [{ tier: "channel", value: "on", locked: false, text: "on" }],
    })]
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    expect(screen.getByRole("combobox", { name: "Progress plans" })).toHaveTextContent("On")
    expect(screen.getByText("Off, set for the workspace")).toBeInTheDocument()
  })

  it("adds nothing when the value set here is the one in effect", () => {
    features = [view("progress_plans", {
      value: "on", source: "channel", summary: "On, set for this channel",
      tiers: [{ tier: "channel", value: "on", locked: false, text: "on" }],
    })]
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    expect(screen.queryByText("On, set for this channel")).not.toBeInTheDocument()
  })

  it("shows a value locked above as read-only", () => {
    features = [view("progress_plans", { value: "off", locked: true, source: "workspace", summary: "Off, locked by the workspace" })]
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    expect(screen.queryByRole("combobox", { name: "Progress plans" })).not.toBeInTheDocument()
    expect(screen.getByText("Off")).toBeInTheDocument()
    expect(screen.getByText("Locked by the workspace. Ask an admin.")).toBeInTheDocument()
  })

  it("shows why a feature is unavailable as a state", () => {
    features = [view("progress_plans", { unavailable: "Needs a sandbox. Ask an admin.", tiers: [{ tier: "channel", value: "off", locked: false, text: "off" }] })]
    render(<FeatureSettings place={{ channel: "c1" }} tier="channel" edit />)
    expect(screen.getByText("Unavailable")).toBeInTheDocument()
    expect(screen.getByText("Needs a sandbox. Ask an admin.")).toBeInTheDocument()
  })
})

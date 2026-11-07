import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { Switches } from "./Switches"
import type { ChannelApprovals, ChannelDetail, ChannelSettings, ChannelSwitch } from "./types"

// The select's pointer handling needs what jsdom leaves out.
Object.assign(Element.prototype, { hasPointerCapture: () => false, releasePointerCapture: () => {}, scrollIntoView: () => {} })

const run = vi.fn()
vi.mock("./api", () => ({ useSave: () => ({ pending: false, run }) }))

const sw = (over: Partial<ChannelSwitch> = {}): ChannelSwitch => ({ available: true, own: null, value: "off", source: "deployment", ...over })
const approvals = (over: Partial<ChannelApprovals> = {}): ChannelApprovals =>
  ({ available: true, own: null, value: "on", source: "default", active: true, editable: true, ...over })
const settings = (over: Partial<ChannelSettings> = {}) =>
  ({ participation: sw(), progress_plans: sw({ value: "on", source: "workspace" }), approvals: approvals(), ...over }) as ChannelSettings
const channel = (can_manage = true) => ({ id: "c1", platform: "discord", can_manage }) as ChannelDetail

describe("Switches", () => {
  beforeEach(() => run.mockReset())

  it("shows one Settings section with approvals, thread participation and progress plans", () => {
    render(<Switches c={channel()} s={settings()} />)
    expect(screen.getByText("Settings")).toBeInTheDocument()
    expect(screen.getByRole("combobox", { name: "Approvals" })).toHaveTextContent("Built-in default (Ask)")
    expect(screen.getByRole("combobox", { name: "Thread participation" })).toHaveTextContent("Built-in default (Off)")
    expect(screen.getByRole("combobox", { name: "Progress plans" })).toHaveTextContent("Workspace default (On)")
  })

  it("says approvals are saved but ignored while overrides are off", () => {
    render(<Switches c={channel()} s={settings({ approvals: approvals({ active: false }) })} />)
    expect(screen.getByText("Saved but ignored until the operator enables approval overrides.")).toBeInTheDocument()
  })

  it("shows approvals read-only to an admin with no linked chat account, and says who can change them", () => {
    render(<Switches c={channel()} s={settings({ approvals: approvals({ editable: false, own: "off", value: "off", source: "channel" }) })} />)
    expect(screen.queryByRole("combobox", { name: "Approvals" })).not.toBeInTheDocument()
    expect(screen.getByText("Allow writes")).toBeInTheDocument()
    expect(screen.getByText(/Only a chat admin signed in with their chat account/)).toBeInTheDocument()
  })

  it("shows members the values only", () => {
    render(<Switches c={channel(false)} s={settings()} />)
    expect(screen.queryAllByRole("combobox")).toHaveLength(0)
    expect(screen.getByText("Ask")).toBeInTheDocument()
  })

  it("sends a new approvals value with the value it read", async () => {
    render(<Switches c={channel()} s={settings()} />)
    await userEvent.click(screen.getByRole("combobox", { name: "Approvals" }))
    await userEvent.click(await screen.findByRole("option", { name: "Allow writes" }))
    expect(run).toHaveBeenCalledWith("put", "approvals", { mode: "off", expected: null })
  })
})

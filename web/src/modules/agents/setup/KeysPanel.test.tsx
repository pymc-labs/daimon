import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { Agent } from "../types"
import { CodingPanel } from "./CodingPanel"
import { KeysPanel } from "./KeysPanel"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const agent = { id: "ag_w", can_view_details: true } as Agent
const props = { node: "keys", agent, config: agent, ro: false, changed: new Set<string>(), update: () => {}, select: () => {} }

const view = (ui: React.ReactNode) =>
  render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><ConfirmProvider>{ui}</ConfirmProvider></TooltipProvider></QueryClientProvider>)

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("KeysPanel", () => {
  it("lists names and takes the value in a masked field", async () => {
    vi.mocked(api.get).mockResolvedValue([{ key: "SERVICE_API_KEY", updated_at: "2026-11-01T00:00:00Z" }] as never)
    view(<KeysPanel {...props} />)
    expect(await screen.findByText("SERVICE_API_KEY")).toBeInTheDocument()
    expect(screen.getByLabelText("Value")).toHaveAttribute("type", "password")
  })

  it("asks first, then sends the value once and clears the form", async () => {
    vi.mocked(api.get).mockResolvedValue([] as never)
    vi.mocked(api.put).mockResolvedValue({ key: "NEW_API_KEY", updated_at: "2026-11-01T00:00:00Z" } as never)
    view(<KeysPanel {...props} />)
    await userEvent.type(screen.getByLabelText("Name"), "NEW_API_KEY")
    await userEvent.type(screen.getByLabelText("Value"), "hunter2")
    await userEvent.click(screen.getByRole("button", { name: "Add" }))
    expect(api.put).not.toHaveBeenCalled()
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Add" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/agents/ag_w/keys/NEW_API_KEY", { value: "hunter2" }))
    await waitFor(() => expect(screen.getByLabelText("Value")).toHaveValue(""))
  })

  it("removes only after a confirm", async () => {
    vi.mocked(api.get).mockResolvedValue([{ key: "SERVICE_API_KEY", updated_at: "2026-11-01T00:00:00Z" }] as never)
    vi.mocked(api.del).mockResolvedValue({ removed: true } as never)
    view(<KeysPanel {...props} />)
    await userEvent.click(await screen.findByRole("button", { name: "Remove SERVICE_API_KEY" }))
    expect(api.del).not.toHaveBeenCalled()
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Remove" }))
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/api/agents/ag_w/keys/SERVICE_API_KEY"))
  })
})

describe("CodingPanel", () => {
  it("shows a new key's token once, and not again after Done", async () => {
    vi.mocked(api.get).mockResolvedValue([] as never)
    vi.mocked(api.post).mockResolvedValue({ id: "k1", token: "tok-shown-once", command: "claude mcp add x tok-shown-once", config: "{}", expires_at: "2026-12-01T00:00:00Z" } as never)
    view(<CodingPanel {...props} node="coding" />)
    await userEvent.click(await screen.findByRole("button", { name: "Make key" }))
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Make key" }))
    expect(await screen.findByText(/tok-shown-once/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Done" }))
    await waitFor(() => expect(screen.queryByText(/tok-shown-once/)).not.toBeInTheDocument())
  })
})

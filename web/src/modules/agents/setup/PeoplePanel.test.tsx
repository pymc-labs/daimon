import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { Agent } from "../types"
import { PeoplePanel } from "./PeoplePanel"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), put: vi.fn() } }))

const agent = {
  id: "ag_w", owner: { id: "p1", name: "Maya" }, co_owners: [{ id: "p2", name: "Jonas" }],
} as Agent
const props = { node: "people", agent, config: agent, ro: false, changed: new Set<string>(), update: () => {}, select: () => {} }

const view = (role: string) => {
  vi.mocked(api.get).mockImplementation(async (path: string) =>
    (path === "/api/me" ? { role, capabilities: [] } : [{ id: "p1", name: "Maya" }, { id: "p2", name: "Jonas" }, { id: "p3", name: "Priya" }]) as never)
  render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><ConfirmProvider><PeoplePanel {...props} /></ConfirmProvider></TooltipProvider></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = () => {}
  Element.prototype.hasPointerCapture = () => false
})

describe("PeoplePanel", () => {
  it("lets a member read the owners and nothing more", async () => {
    view("member")
    expect(await screen.findByText("Maya")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument()
  })

  it("lets an admin drop a co-owner and save after a confirm", async () => {
    vi.mocked(api.put).mockResolvedValue(undefined as never)
    view("admin")
    const save = await screen.findByRole("button", { name: "Save" })
    expect(save).toBeDisabled()
    await userEvent.click(screen.getByRole("button", { name: "Remove Jonas" }))
    expect(save).toBeEnabled()
    await userEvent.click(save)
    expect(api.put).not.toHaveBeenCalled()
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Change" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/agents/ag_w/owners", { owner: "p1", co_owners: [] }))
  })
})

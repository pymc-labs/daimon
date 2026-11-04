import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { ConnectedAccount } from "@/lib/types"
import { type DeletePreview, type Profile, ProfilePage } from "./Profile"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const daimon = { id: "ag_daimon", name: "Daimon" }
const data: Profile = {
  name: "Maya Lindqvist",
  memory: [{ agent: daimon, files: ["/style.md"] }],
  connections: [{ kind: "mcp", label: "Linear", agent: daimon, key: "https://mcp.linear.app/sse" }],
  sign_ins: [{ id: "i1", provider: "microsoft", label: "maya@fernhill.example" }],
  chat_accounts: [],
  keys: [{ id: "k1", agent: daimon, created_at: "2026-11-01T00:00:00Z", expires_at: null, bound: false }],
}
const accounts: ConnectedAccount[] = [
  { id: "a1", provider: "discord", provider_label: "Discord", label: "maya.l", last_used: "2026-11-04T08:00:00Z", current: true },
  { id: "a2", provider: "slack", provider_label: "Slack", label: "maya@northwind", last_used: "2026-11-01T08:00:00Z", current: false },
]
const preview: DeletePreview = { enabled: true, held: ["2 routines"], kept: ["3 conversations shared with other people"] }

const view = () => {
  vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/profile" ? data : path === "/api/me" ? { workspace: { name: "Fernhill" }, capabilities: [], accounts } : preview) as never)
  vi.mocked(api.post).mockResolvedValue({} as never)
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><TooltipProvider><ConfirmProvider><ProfilePage /></ConfirmProvider></TooltipProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("Profile", () => {
  it("lists what is held and forgets an agent's memory after a confirm", async () => {
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Forget Daimon" }))
    await userEvent.click(await screen.findByRole("button", { name: "Forget" }))
    expect(api.post).toHaveBeenCalledWith("/api/profile/memory/ag_daimon/forget")
  })

  it("disconnects a connected account but never the one signed in with", async () => {
    view()
    expect(await screen.findByText("Signed in")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Disconnect Discord maya.l" })).toBeNull()
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect Slack maya@northwind" }))
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }))
    expect(api.del).toHaveBeenCalledWith("/api/me/accounts/a2")
  })

  it("keeps the only sign-in", async () => {
    view()
    expect(await screen.findByRole("button", { name: "Remove maya@fernhill.example" })).toBeDisabled()
  })

  it("deletes only once the name is typed", async () => {
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Delete my data" }))
    expect(await screen.findByText("2 routines")).toBeInTheDocument()
    expect(screen.getByText("3 conversations shared with other people")).toBeInTheDocument()
    const go = screen.getByRole("button", { name: "Delete" })
    expect(go).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type Maya Lindqvist/), "maya lindqvist")
    expect(go).toBeEnabled()
  })
})

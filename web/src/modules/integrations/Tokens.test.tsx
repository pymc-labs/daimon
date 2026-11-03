import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { Tokens } from "./Tokens"
import type { Tokens as Data } from "./types"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const data: Data = {
  configured: true, days: 30,
  scopes: [{ id: "tenant:read", label: "Read the workspace summary" }, { id: "channels:write", label: "Change channel settings" }],
  tokens: [{ id: "tk1", label: "Status dashboard", scopes: ["tenant:read"], created_at: "2026-10-30T00:00:00Z", expires_at: "2026-11-29T00:00:00Z" }],
}

const view = (caps: string[]) => {
  vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/me" ? { capabilities: caps } : data) as never)
  render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><Tokens /></TooltipProvider></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("Tokens", () => {
  it("lists tokens by name and permission", async () => {
    view(["integrations.view"])
    expect(await screen.findByText("Status dashboard")).toBeInTheDocument()
    expect(screen.getByText("Read the workspace summary")).toBeInTheDocument()
  })

  it("offers no way to create or revoke without the manage right", async () => {
    view(["integrations.view"])
    await screen.findByText("Status dashboard")
    expect(screen.queryByRole("button", { name: /New token/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument()
  })

  it("keeps Create off until a permission is picked, then shows the token once", async () => {
    vi.mocked(api.post).mockResolvedValue({ token: "shown-once-value", id: "tk2", scopes: ["channels:write"], expires_at: "2026-12-01T00:00:00Z" })
    view(["integrations.view", "integrations.manage"])
    await userEvent.click(await screen.findByRole("button", { name: /New token/ }))
    const create = screen.getByRole("button", { name: "Create" })
    expect(create).toBeDisabled()
    await userEvent.type(screen.getByLabelText("Name"), "  CI  ")
    await userEvent.click(screen.getByRole("checkbox", { name: "Change channel settings" }))
    await userEvent.click(create)
    expect(await screen.findByText("shown-once-value")).toBeInTheDocument()
    expect(api.post).toHaveBeenCalledWith("/api/integrations/tokens", { scopes: ["channels:write"], label: "CI" })
    await userEvent.click(screen.getByRole("button", { name: "Done" }))
    await waitFor(() => expect(screen.queryByText("shown-once-value")).not.toBeInTheDocument())
  })
})

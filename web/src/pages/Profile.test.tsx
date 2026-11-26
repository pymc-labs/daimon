import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { ConnectedAccount } from "@/lib/types"
import type { Connected } from "@/modules/data/types"
import { type DeletePreview, type Profile, ProfilePage } from "./Profile"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const daimon = { id: "ag_daimon", name: "Daimon" }
const data: Profile = {
  name: "Maya Lindqvist",
  memory: [{ agent: daimon, files: ["/style.md"] }],
  connections: [{ kind: "mcp", label: "Linear", agent: daimon, key: "https://mcp.linear.app/sse" }],
  sign_ins: [{ id: "i1", provider: "microsoft", label: "maya@fernhill.example" }],
  chat_accounts: [],
  keys: [{ id: "k1", agent: daimon, client: "codex", created_at: "2026-11-01T00:00:00Z", expires_at: null, bound: false }],
}
const accounts: ConnectedAccount[] = [
  { id: "a1", provider: "discord", provider_label: "Discord", label: "maya.l", last_used: "2026-11-04T08:00:00Z", current: true },
  { id: "a2", provider: "slack", provider_label: "Slack", label: "maya@northwind", last_used: "2026-11-01T08:00:00Z", current: false },
]
const preview: DeletePreview = { enabled: true, scope: ["It tries the same at OpenAI."], held: ["2 routines"], kept: ["3 conversations shared with other people"] }

const dataAccounts: Connected = {
  notices: [{ kind: "linked", identity: "slack:T1:user:U1", provider: "slack", name: "maya" }],
  accounts: [{ provider: "slack", workspace: "T1", name: "maya", email: null, proof: "admin", linked_at: "2026-11-01T00:00:00Z" }],
  connections: [{ source_id: "s1", source: "Shared drive", provider: "google", subject: "maya@fernhill.example", status: "ok", last_ok_at: null }],
  connectable: [{ source_id: "s1", name: "Shared drive", provider: "google" }, { source_id: "s2", name: "Support inbox", provider: "google" }],
  personal: { drive: true },
}

const view = (modules: string[] = []) => {
  vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/profile" ? data : path === "/api/profile/data" ? dataAccounts : path === "/api/me" ? { workspace: { name: "Fernhill" }, capabilities: [], modules, accounts } : preview) as never)
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
    expect(screen.getByText("Sign-ins")).toBeInTheDocument()
    expect(screen.getByText("Connected accounts")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Disconnect Discord maya.l" })).toBeNull()
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect Slack maya@northwind" }))
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }))
    expect(api.del).toHaveBeenCalledWith("/api/me/accounts/a2")
  })

  it("lists data accounts and link notices in the one Connected accounts list", async () => {
    view(["data"])
    const list = await screen.findByRole("region", { name: "Connected accounts" })
    expect(await within(list).findByText(/An admin linked maya to your account/)).toBeInTheDocument()
    expect(screen.queryByText("Data accounts")).toBeNull()
    expect(within(list).getByRole("button", { name: "Connect Support inbox" })).toBeInTheDocument()
    expect(within(list).queryByRole("button", { name: "Connect Shared drive" })).toBeNull()
    expect(within(list).getByRole("switch", { name: "Read my Google Drive as me" })).toBeChecked()
    expect(within(list).queryByRole("switch", { name: "Read my calendar as me" })).toBeNull()
    await userEvent.click(within(list).getByRole("button", { name: "Disconnect Shared drive" }))
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }))
    expect(api.del).toHaveBeenCalledWith("/api/profile/data/connections/s1")
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
    expect(screen.getByText("It tries the same at OpenAI.")).toBeInTheDocument()
    const go = screen.getByRole("button", { name: "Delete" })
    expect(go).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type Maya Lindqvist/), "maya lindqvist")
    expect(go).toBeEnabled()
  })
})

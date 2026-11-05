import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import type { Me } from "@/lib/types"
import { UserMenu } from "./Sidebar"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock("@/lib/theme-context", () => ({ useTheme: () => ({ theme: "light", setTheme: vi.fn() }) }))

const me = (over: Partial<Me> = {}) => ({
  id: "p1", name: "Maya Lindqvist", email: "maya@fernhill.example", role: "owner", workspaces: [], identities: [],
  accounts: [{ id: "a1", provider: "discord", provider_label: "Discord", label: "maya.l", last_used: "2026-11-04T08:00:00Z", current: true }],
  ...over,
}) as unknown as Me

const open = async (m: Me) => {
  vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/me" ? m : [
    { id: "discord", label: "Discord", kind: "redirect" },
    { id: "slack", label: "Slack", kind: "redirect" },
    { id: "microsoft", label: "Microsoft", kind: "redirect" },
  ]) as never)
  const view = render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><UserMenu /></MemoryRouter></QueryClientProvider>)
  await userEvent.click(await screen.findByText("Maya Lindqvist"))
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("UserMenu", () => {
  it("offers only the providers not yet connected and starts a connect", async () => {
    vi.mocked(api.post).mockResolvedValue({ url: "/chat" } as never)
    vi.stubGlobal("location", { ...location, assign: vi.fn() })
    await open(me())
    await userEvent.click(await screen.findByText("Connect account"))
    expect(screen.queryByRole("menuitem", { name: "Discord" })).toBeNull()
    await userEvent.click(await screen.findByRole("menuitem", { name: "Slack" }))
    expect(api.post).toHaveBeenCalledWith("/auth/slack/connect", { next: "/chat" })
  })

  it("hides the menu once every provider is connected", async () => {
    const all = ["discord", "slack", "microsoft"].map((p) => ({ id: p, provider: p, provider_label: p, label: p, last_used: "", current: p === "discord" }))
    await open(me({ accounts: all }))
    expect(screen.queryByText("Connect account")).toBeNull()
  })

  it("shows the email under the name, and nothing when there is none", async () => {
    const { unmount } = await open(me())
    expect(screen.getByText("maya@fernhill.example")).toBeTruthy()
    unmount()
    const { container } = await open(me({ email: "" }))
    expect(container.querySelectorAll("span.text-xs")).toHaveLength(0)
  })

  it("hides the menu from guests", async () => {
    await open(me({ role: "visitor" }))
    expect(screen.queryByText("Connect account")).toBeNull()
  })
})

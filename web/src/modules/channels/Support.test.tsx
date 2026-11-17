import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api, ApiError } from "@/lib/api"
import { SupportTab } from "./Support"
import type { SupportPlans } from "./types"

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), put: vi.fn() } }))

const plan = { scope: "channel" as const, scope_id: "c1", catalog: "support", pool: "5", per_person: "2", window: "monthly" as const, starts_at: null, ends_at: null }
const data: SupportPlans = {
  platform: "discord", default_credits: "20",
  default_allowances: [{ platform_user_id: "u2", name: "Ana", credits: "0.5" }],
  plans: [{ ...plan, name: "general", pool_used: "1", state: "active", summary: "1 of 5 credits used, 2 per person", allowances: [{ platform_user_id: "u1", name: null, credits: "4" }] }],
  catalogs: [{ key: "support", name: "Support", revision: 3, summary: "Review (1-3 credits)", catalog: { name: "Support", options: [] } }],
  agents: [{ id: "daimon", name: "Daimon" }],
}

const view = () => {
  vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/channels" ? { platform: "discord", channels: [] } : data) as never)
  vi.mocked(api.put).mockResolvedValue(data as never)
  render(<QueryClientProvider client={new QueryClient()}><SupportTab /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("SupportTab", () => {
  it("shows plans, own caps and menus", async () => {
    view()
    expect(await screen.findByText("1 of 5 credits used, 2 per person")).toBeInTheDocument()
    expect(screen.getByText("20 credits per person")).toBeInTheDocument()
    expect(screen.getByText("Ana")).toBeInTheDocument()
    expect(screen.getByText("u1")).toBeInTheDocument()
    expect(screen.getByText("Review (1-3 credits)")).toBeInTheDocument()
  })

  it("adds a workspace plan with nothing expected", async () => {
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Add plan" }))
    expect(screen.getByRole("radio", { name: "Workspace" })).toHaveAttribute("aria-checked", "true")
    await userEvent.type(screen.getByLabelText("Shared credits"), "10")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenCalledWith("/api/support-plans/plan", {
      scope: "workspace", scope_id: "", expected: null,
      plan: { catalog: null, pool: "10", per_person: null, window: "total", starts_at: null, ends_at: null },
    })
  })

  it("sends the plan as read and says when it changed meanwhile", async () => {
    view()
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(409, "stale"))
    const plans = (await screen.findByRole("heading", { name: "Plans" })).closest("section")!
    await userEvent.click(within(plans).getByRole("button", { name: "Edit" }))
    await userEvent.clear(screen.getByLabelText("Credits per person"))
    await userEvent.type(screen.getByLabelText("Credits per person"), "3")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenCalledWith("/api/support-plans/plan", expect.objectContaining({ expected: plan, plan: expect.objectContaining({ per_person: "3", catalog: "support" }) }))
    expect(await screen.findByRole("alert")).toHaveTextContent("Changed meanwhile. Reloaded.")
  })

  it("clears a cap under its plan with the credits as read", async () => {
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Clear u1" }))
    expect(api.put).toHaveBeenCalledWith("/api/support-plans/allowances", { platform_user_id: "u1", credits: null, scope: "channel", scope_id: "c1", expected: "4" })
  })

  it("keeps a menu that is not JSON off the server", async () => {
    view()
    const menus = (await screen.findByRole("heading", { name: "Menus" })).closest("section")!
    await userEvent.click(within(menus).getByRole("button", { name: "Edit" }))
    await userEvent.clear(screen.getByLabelText("Menu JSON"))
    await userEvent.type(screen.getByLabelText("Menu JSON"), "nope")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(screen.getByRole("alert")).toHaveTextContent("Not valid JSON")
    expect(api.put).not.toHaveBeenCalled()
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument()
  })
})

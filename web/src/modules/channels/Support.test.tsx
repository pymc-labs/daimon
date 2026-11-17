import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor, within } from "@testing-library/react"
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

let current = data
let qc: QueryClient
const view = () => {
  current = data
  qc = new QueryClient()
  vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/channels" ? { platform: "discord", channels: [] } : current) as never)
  vi.mocked(api.put).mockResolvedValue(data as never)
  render(<QueryClientProvider client={qc}><SupportTab /></QueryClientProvider>)
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

  it("reloads the plan form after a 409, so a second save cannot overwrite the other change", async () => {
    view()
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(409, "stale"))
    const plans = (await screen.findByRole("heading", { name: "Plans" })).closest("section")!
    await userEvent.click(within(plans).getByRole("button", { name: "Edit" }))
    const theirs = { ...data.plans[0], per_person: "4", pool: "9" }
    current = { ...data, plans: [theirs] }
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("Changed meanwhile. Reloaded.")
    expect(screen.getByLabelText("Credits per person")).toHaveValue("4")
    expect(screen.getByLabelText("Shared credits")).toHaveValue("9")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenLastCalledWith("/api/support-plans/plan", expect.objectContaining({ expected: { ...plan, per_person: "4", pool: "9" } }))
  })

  it("expects the plan as it was when the form opened, not as refetched since", async () => {
    view()
    const plans = (await screen.findByRole("heading", { name: "Plans" })).closest("section")!
    await userEvent.click(within(plans).getByRole("button", { name: "Edit" }))
    current = { ...data, plans: [{ ...data.plans[0], pool: "9", summary: "changed" }] }
    await qc.invalidateQueries({ queryKey: ["support-plans"] })
    expect(await screen.findByText("changed")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenCalledWith("/api/support-plans/plan", expect.objectContaining({ expected: plan }))
  })

  it("labels the date inputs with their time zone", async () => {
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Add plan" }))
    expect(screen.getByLabelText("Counting from (UTC)")).toHaveAttribute("type", "date")
  })

  it("reloads the cap form after a 409 with the credits now there", async () => {
    view()
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(409, "stale"))
    await userEvent.click(await screen.findByRole("button", { name: "Add cap" }))
    await userEvent.type(screen.getByLabelText("User id"), "u2")
    await userEvent.type(screen.getByLabelText("Credits"), "1")
    current = { ...data, default_allowances: [{ platform_user_id: "u2", name: "Ana", credits: "3" }] }
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    await waitFor(() => expect(screen.getByLabelText("Credits")).toHaveValue("3"))
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenLastCalledWith("/api/support-plans/allowances", expect.objectContaining({ platform_user_id: "u2", credits: "3", expected: "3" }))
  })

  it("clears a cap under its plan with the credits as read", async () => {
    view()
    await userEvent.click(await screen.findByRole("button", { name: "Clear u1" }))
    expect(api.put).toHaveBeenCalledWith("/api/support-plans/allowances", { platform_user_id: "u1", credits: null, scope: "channel", scope_id: "c1", expected: "4" })
  })

  it("sends the menu rows as a catalog with the revision read", async () => {
    view()
    const menus = (await screen.findByRole("heading", { name: "Menus" })).closest("section")!
    await userEvent.click(within(menus).getByRole("button", { name: "Edit" }))
    expect(screen.getByLabelText("Menu name")).toHaveValue("Support")
    await userEvent.type(screen.getByLabelText("Key of option 1"), "Review Thread")
    await userEvent.type(screen.getByLabelText("Label of option 1"), "Review this thread")
    await userEvent.clear(screen.getByLabelText("Credits of option 1"))
    await userEvent.type(screen.getByLabelText("Credits of option 1"), "0.5")
    await userEvent.click(screen.getByRole("button", { name: "Add field to option 1" }))
    await userEvent.type(screen.getByLabelText("Label of field 1 of option 1"), "What to look at")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenCalledWith("/api/support-plans/catalogs/support", {
      expected_revision: 3,
      catalog: { name: "Support", options: [{
        key: "review_thread", label: "Review this thread", price: { kind: "fixed", amount: "0.5" },
        fields: [{ key: "what_to_look_at", label: "What to look at", kind: "text" }],
      }] },
    })
  })

  it("shows a server refusal next to the input it names", async () => {
    view()
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(422, "Catalog not valid: options.0.label: String should have at least 1 character; options.0.price.fixed.amount: amount must be more than 0 and at most 1000; options: something odd"))
    const menus = (await screen.findByRole("heading", { name: "Menus" })).closest("section")!
    await userEvent.click(within(menus).getByRole("button", { name: "Edit" }))
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    const near = (label: string) => screen.getByLabelText(label).parentElement!
    expect(near("Label of option 1")).toHaveTextContent("String should have at least 1 character")
    expect(near("Credits of option 1")).toHaveTextContent("amount must be more than 0")
    expect(screen.getByLabelText("Label of option 1")).toBeInvalid()
  })

  it("reloads the menu form after a 409 and expects the new revision", async () => {
    view()
    vi.mocked(api.put).mockRejectedValueOnce(new ApiError(409, "stale"))
    const menus = (await screen.findByRole("heading", { name: "Menus" })).closest("section")!
    await userEvent.click(within(menus).getByRole("button", { name: "Edit" }))
    const theirs = { name: "Their menu", options: [{ key: "call", label: "Call", price: { kind: "fixed", amount: "2" } }] }
    current = { ...data, catalogs: [{ ...data.catalogs[0], revision: 4, catalog: theirs }] }
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("Changed meanwhile. Reloaded.")
    expect(screen.getByLabelText("Menu name")).toHaveValue("Their menu")
    expect(screen.getByLabelText("Label of option 1")).toHaveValue("Call")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(api.put).toHaveBeenLastCalledWith("/api/support-plans/catalogs/support", { catalog: theirs, expected_revision: 4 })
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument()
  })
})

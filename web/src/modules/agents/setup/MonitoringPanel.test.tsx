import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import type { Agent } from "../types"
import { MonitoringPanel } from "./MonitoringPanel"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), put: vi.fn() },
}))

const agent = { id: "ag_w" } as unknown as Agent
const schema = (key: string, name: string) => ({
  key, name, category: "guardrails", categoryName: "Guardrails", values: [{ value: "on", label: "On" }, { value: "off", label: "Off" }],
  default: "off", tiers: ["deployment", "workspace", "agent"], lockable: [], platforms: [], threadAnyone: false, native: false, doc: "",
})
const view = (key: string, over: object = {}) => ({
  key, tiers: [{ tier: "agent", value: "default", locked: false, text: "default" }], value: "off", source: "default", locked: false, unavailable: null, summary: "Off", ...over,
})

function serve(features: object[]) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/api/features/schema") return [schema("monitor_direct_messages", "Monitor direct messages"), schema("monitor_mcp", "Monitor MCP")]
    if (path.startsWith("/api/features")) return { platform: "discord", features }
    return { id: "ag_w", name: "W", direct_messages: { on: false, since: null }, mcp: { on: false, since: null } }
  })
}
const show = () => render(
  <QueryClientProvider client={new QueryClient()}><MemoryRouter><MonitoringPanel agent={agent} /></MemoryRouter></QueryClientProvider>,
)

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = () => {}
  Element.prototype.hasPointerCapture = () => false
})

describe("MonitoringPanel", () => {
  it("turns a switch on at the agent with the value it read", async () => {
    serve([view("monitor_direct_messages"), view("monitor_mcp")])
    vi.mocked(api.put).mockResolvedValue({} as never)
    show()
    await userEvent.click(await screen.findByRole("combobox", { name: "Direct messages" }))
    await userEvent.click(await screen.findByRole("option", { name: "On" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/features?agent=ag_w", { key: "monitor_direct_messages", tier: "agent", value: "on", locked: false, expected: "default" }))
  })

  it("shows a lock from above instead of a control, and links to the agent's activity", async () => {
    serve([view("monitor_direct_messages", { locked: true, source: "workspace", value: "on", summary: "On" }), view("monitor_mcp")])
    show()
    expect(await screen.findByText("Locked by the workspace")).toBeInTheDocument()
    expect(screen.queryByRole("combobox", { name: "Direct messages" })).toBeNull()
    expect(screen.getByRole("link", { name: "View activity" })).toHaveAttribute("href", "/admin/monitoring?agent=ag_w")
  })
})

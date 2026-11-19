import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError, api } from "@/lib/api"
import type { Agent, AgentConfig } from "../types"
import { Inspector } from "./Inspector"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), put: vi.fn() },
}))
vi.mock("@/lib/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/hooks")>()),
  useCan: () => () => true,
}))
vi.mock("../usePendingPreview", () => ({ usePendingPreview: () => ({ data: undefined }) }))

const agent = { id: "ag_w", model: { backend: "managed", id: "m" }, nodes: [] } as unknown as Agent
const props = { node: "features", agent, config: agent as unknown as AgentConfig, ro: false, changed: new Set<string>(), update: vi.fn(), select: vi.fn() }
const KEYS = [["monitor_direct_messages", "Monitor direct messages"], ["monitor_mcp", "Monitor coding tools"]]
const schema = ([key, name]: string[]) => ({
  key, name, category: "guardrails", categoryName: "Guardrails", values: [{ value: "on", label: "On" }, { value: "off", label: "Off" }],
  default: "off", tiers: ["deployment", "workspace", "agent"], lockable: [], platforms: [], threadAnyone: false, native: false, doc: "",
})
const view = ([key]: string[]) => ({ key, tiers: [{ tier: "agent", value: "default", locked: false, text: "default" }], value: "off", source: "default", locked: false, unavailable: null, summary: "Off" })

function serve(reads: boolean) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/api/features/schema") return KEYS.map(schema)
    if (path.startsWith("/api/features")) return { platform: "discord", features: KEYS.map(view) }
    if (!reads) throw new ApiError(403, "Only workspace admins can read monitored conversations.")
    return { id: "ag_w", name: "W", direct_messages: { on: true, since: "2026-05-01T09:00:00Z" }, coding_tools: { on: false, since: null } }
  })
}
const show = () => render(
  <QueryClientProvider client={new QueryClient()}><MemoryRouter><Inspector {...props} /></MemoryRouter></QueryClientProvider>,
)

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = () => {}
})

describe("Inspector features panel monitoring rows", () => {
  it("lists the two switches, with since and the activity link for a workspace admin", async () => {
    serve(true)
    show()
    for (const [, name] of KEYS) expect(await screen.findByRole("combobox", { name })).toBeInTheDocument()
    expect(await screen.findByText(/^Since /)).toBeInTheDocument()
    expect(await screen.findByRole("link", { name: "View activity" })).toHaveAttribute("href", "/admin/monitoring?agent=ag_w")
  })

  it("shows the switches without the admin extras to an agent owner who is no workspace admin", async () => {
    serve(false)
    show()
    expect(await screen.findByRole("combobox", { name: "Monitor coding tools" })).toBeInTheDocument()
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/admin/monitoring/agents/ag_w"))
    expect(screen.queryByRole("link", { name: "View activity" })).toBeNull()
  })
})

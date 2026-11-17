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
const props = { node: "monitoring", agent, config: agent as unknown as AgentConfig, ro: false, changed: new Set<string>(), update: vi.fn(), select: vi.fn() }
const schema = (key: string) => ({
  key, name: key, category: "guardrails", categoryName: "Guardrails", values: [{ value: "on", label: "On" }, { value: "off", label: "Off" }],
  default: "off", tiers: ["deployment", "workspace", "agent"], lockable: [], platforms: [], threadAnyone: false, native: false, doc: "",
})
const view = (key: string) => ({ key, tiers: [{ tier: "agent", value: "default", locked: false, text: "default" }], value: "off", source: "default", locked: false, unavailable: null, summary: "Off" })

function serve(reads: boolean) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    if (path === "/api/features/schema") return [schema("monitor_direct_messages"), schema("monitor_mcp")]
    if (path.startsWith("/api/features")) return { platform: "discord", features: [view("monitor_direct_messages"), view("monitor_mcp")] }
    if (!reads) throw new ApiError(403, "Only platform admins can read monitored conversations.")
    return { id: "ag_w", name: "W", direct_messages: { on: false, since: null }, coding_tools: { on: false, since: null } }
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

describe("Inspector monitoring panel", () => {
  it("shows the switches to a platform admin who can read the state", async () => {
    serve(true)
    show()
    expect(await screen.findByRole("combobox", { name: "Direct messages" })).toBeInTheDocument()
  })

  it("shows nothing to a workspace owner who may not read the state", async () => {
    serve(false)
    const { container } = show()
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/admin/monitoring/agents/ag_w"))
    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(screen.queryByRole("combobox", { name: "Direct messages" })).toBeNull()
  })
})

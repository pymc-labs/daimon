import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { renderAt } from "@/test/render"
import { Monitoring } from "./Monitoring"
import type { AgentMonitoring, MonitoredConversation, MonitoredEvent } from "./types"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), page: vi.fn() },
}))

const conversation: MonitoredConversation = {
  id: "mc1", person: { id: "p1", name: "Owen Brooks" }, where: "mcp", questions: 2, tools: 1,
  first_at: "2026-03-10T10:00:00Z", last_at: "2026-03-10T11:00:00Z",
}
const events: MonitoredEvent[] = [
  { id: "e1", at: "2026-03-10T10:00:00Z", kind: "question", content: "What did we charge Brightwave?", tool: null, outcome: null },
  { id: "e2", at: "2026-03-10T10:00:05Z", kind: "tool", content: null, tool: "hubspot_search_crm", outcome: "denied" },
  { id: "e3", at: "2026-03-10T10:00:09Z", kind: "answer", content: "Eighteen thousand dollars.", tool: null, outcome: null },
]
const state = (on: boolean): AgentMonitoring => ({ id: "ag1", name: "Pricing", direct_messages: { on, since: null }, mcp: { on, since: null } })

function serve(rows: MonitoredConversation[], on = true) {
  vi.mocked(api.get).mockImplementation(async (path: string) =>
    path === "/api/me" ? { capabilities: ["workspace.manage"], workspace: { name: "Workspace" } }
      : path === "/api/agents" ? [{ id: "ag1", name: "Pricing", avatar: "", model_label: "Opus", purpose: "" }] : state(on))
  vi.mocked(api.page).mockImplementation(async (path: string) => ({
    rows: path.includes("/conversations/") ? events : path.includes("/conversations") ? rows : [{ id: "p1", name: "Owen Brooks" }], next: null,
  }))
}
const view = (url: string) => renderAt(url, "/admin/monitoring", <TooltipProvider><Monitoring /></TooltipProvider>)
const pageCalls = () => vi.mocked(api.page).mock.calls.map(([p]) => p)

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = () => {}
  Element.prototype.hasPointerCapture = () => false
})

describe("Monitoring", () => {
  it("asks for the filters in the address and nothing else", async () => {
    serve([conversation])
    view("/admin/monitoring?agent=ag1&person=p1&where=mcp&from=2026-03-01&to=2026-03-31")
    await screen.findByText("Owen Brooks")
    const path = pageCalls().find((p) => p.includes("/conversations"))!
    expect(path.startsWith("/api/admin/monitoring/agents/ag1/conversations?")).toBe(true)
    expect(new URL(path, "http://x").searchParams.toString()).toBe("person=p1&where=mcp&from=2026-03-01&to=2026-03-31")
  })

  it("opens a conversation with its questions, answers and tool outcomes", async () => {
    serve([conversation])
    view("/admin/monitoring?agent=ag1")
    await userEvent.click(await screen.findByText("Owen Brooks"))
    expect(await screen.findByText("What did we charge Brightwave?")).toBeInTheDocument()
    expect(screen.getByText("Eighteen thousand dollars.")).toBeInTheDocument()
    expect(screen.getByText("hubspot_search_crm")).toBeInTheDocument()
    expect(screen.getByText("Denied")).toBeInTheDocument()
    await waitFor(() => expect(pageCalls()).toContain("/api/admin/monitoring/agents/ag1/conversations/mc1"))
  })

  it("points to the agent's switches when monitoring is off and nothing is kept", async () => {
    serve([], false)
    view("/admin/monitoring?agent=ag1")
    expect(await screen.findByText("Monitoring is off for this agent")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Open monitoring" })).toHaveAttribute("href", "/agents/ag1?node=monitoring")
  })
})

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ReactFlowProvider } from "@xyflow/react"
import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { Canvas } from "./Canvas"
import type { AgentGithub } from "./setup/github"
import type { Agent } from "./types"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }))

const AT = "2026-11-01T00:00:00Z"
const github: AgentGithub = {
  configured: true, can_manage: true, can_connect: true, skills: [], release: { name: "v2.4.1", at: AT, url: null }, sync: null,
  repos: [{ name: "acme/data", url: "https://github.com/acme/data", access: "write", working: true, shared: false, status: "active", installation: "active", granted_at: AT, granted_by: "Ada" }],
}

const config = {
  name: "Analyst", purpose: "", avatar: "", model: { id: "claude-sonnet-5-5", effort: null }, system: "", skills: [], tools: [], mcp_servers: [], places: [], helpers: [],
}
const show = (details: boolean) => {
  const agent = { ...config, id: "ag_w", can_view_details: details, nodes: [], owner: { id: "p_ada", name: "Ada" }, co_owners: [] } as unknown as Agent
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><TooltipProvider><ReactFlowProvider>
        <Canvas agent={agent} config={agent} changed={new Set()} changedNodes={new Set()} selected="" select={() => {}} ro={false} />
      </ReactFlowProvider></TooltipProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(api.get).mockImplementation(async (path: string) => (path.endsWith("/github") ? github : path === "/api/agents" ? [] : path.includes("/knowledge") ? { shared: false, channels: [] } : []) as never)
})

describe("Canvas GitHub card", () => {
  it("shows the working repo and the release to someone who sees setup", async () => {
    show(true)
    const card = await screen.findByLabelText("GitHub")
    expect(await screen.findByText("acme/data")).toBeInTheDocument()
    expect(card).toHaveTextContent("v2.4.1")
  })

  it("is absent for someone who does not", async () => {
    show(false)
    await screen.findByLabelText("Skills")
    expect(screen.queryByLabelText("GitHub")).toBeNull()
    expect(api.get).not.toHaveBeenCalledWith("/api/agents/ag_w/github")
  })
})

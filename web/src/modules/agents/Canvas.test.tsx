import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ReactFlowProvider } from "@xyflow/react"
import { fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { Canvas } from "./Canvas"
import type { AgentGithub } from "./setup/github"
import type { Agent } from "./types"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }))

const AT = "2026-11-01T00:00:00Z"
const github: AgentGithub = {
  configured: true, can_manage: true, can_connect: true, skills: [], release: { name: "v2.4.1", at: AT, url: null }, sync: { state: "done", at: AT, error: null },
  repos: [{ name: "acme/data", url: "https://github.com/acme/data", access: "write", working: true, shared: false, status: "active", installation: "active", granted_at: AT, granted_by: "Ada" }],
}

const config = {
  name: "Analyst", purpose: "", avatar: "", model: { id: "claude-sonnet-5-5", effort: null }, system: "", skills: [], tools: [], mcp_servers: [], places: [], helpers: [],
}
const show = (details: boolean, extra: object = {}, select = () => {}) => {
  const agent = { ...config, ...extra, id: "ag_w", can_view_details: details, nodes: [], owner: { id: "p_ada", name: "Ada" }, co_owners: [] } as unknown as Agent
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><TooltipProvider><ReactFlowProvider>
        <Canvas agent={agent} config={agent} changed={new Set()} changedNodes={new Set()} selected="" select={select} ro={false} />
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
  it("shows the working repo and the sync to someone who sees setup", async () => {
    show(true)
    const card = await screen.findByLabelText("GitHub")
    expect(await screen.findByText("acme/data")).toBeInTheDocument()
    expect(card).toHaveTextContent(/Synced/)
    expect(card).not.toHaveTextContent("v2.4.1")
  })

  it("is absent for someone who does not", async () => {
    show(false)
    await screen.findByLabelText("Skills")
    expect(screen.queryByLabelText("GitHub")).toBeNull()
    expect(api.get).not.toHaveBeenCalledWith("/api/agents/ag_w/github")
  })
})

describe("Canvas Access via MCP card", () => {
  it("is named for the connection, not the tools", async () => {
    show(true)
    expect(await screen.findByLabelText("Access via MCP")).toBeInTheDocument()
    expect(screen.queryByLabelText("Coding tools")).toBeNull()
  })
})

describe("Canvas card fold", () => {
  afterEach(() => vi.restoreAllMocks())

  it("folds a long card behind Show more, and opening it selects nothing", async () => {
    // jsdom has no layout: rows are 32px and stack, a card body is as tall as its rows, and card bodies
    // (the only observed elements without a class; React Flow's own have one) are measured at once.
    vi.stubGlobal("ResizeObserver", class {
      cb: () => void
      constructor(cb: () => void) { this.cb = cb }
      observe(el: Element) { if (!el.className) this.cb() }
      unobserve() {}
      disconnect() {}
    })
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.row ? 32 : this.querySelectorAll("[data-row]").length * 32
    })
    vi.spyOn(HTMLElement.prototype, "offsetTop", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.row ? [...this.parentElement!.children].indexOf(this) * 32 : 0
    })
    const tools = Array.from({ length: 20 }, (_, i) => ({ name: `tool_${i}`, description: "", effect: "read", rule: "allow" }))
    const select = vi.fn()
    show(false, { mcp_servers: [{ key: "big", name: "Big", url: "https://mcp.example.com", auth: "token", credential: "connected", tools }] }, select)
    // React Flow hides cards it has not measured, so find the button by its text.
    fireEvent.click(await screen.findByText("Show 13 more"))
    expect(await screen.findByText("Show less"), "the card opens").toBeInTheDocument()
    expect(select, "the button is not a click on the card").not.toHaveBeenCalled()
    expect(screen.queryByText(/^Show (\d+ )?more$/), "short cards never fold").toBeNull()
  })
})

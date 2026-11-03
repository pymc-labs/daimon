import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import type { Agent } from "../types"
import { KnowledgePanel } from "./KnowledgePanel"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }))

const agent = { id: "ag_w", can_view_details: true } as Agent
const props = { node: "knowledge", agent, config: agent, ro: true, changed: new Set<string>(), update: () => {}, select: () => {} }

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("KnowledgePanel", () => {
  it("lists the shared files and reads one, with nothing to change", async () => {
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url.endsWith("/knowledge")) return { shared: true, channels: [] } as never
      if (url.includes("/knowledge/paths")) return ["/style.md"] as never
      return { path: "/style.md", content: "Use tabs." } as never
    })
    render(<QueryClientProvider client={new QueryClient()}><KnowledgePanel {...props} /></QueryClientProvider>)
    await userEvent.click(await screen.findByRole("button", { name: "/style.md" }))
    expect(await screen.findByText("Use tabs.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /delete|remove|save/i })).toBeNull()
  })

  it("says so when the agent remembers nothing", async () => {
    vi.mocked(api.get).mockResolvedValue({ shared: false, channels: [] } as never)
    render(<QueryClientProvider client={new QueryClient()}><KnowledgePanel {...props} /></QueryClientProvider>)
    expect(await screen.findByText("None")).toBeInTheDocument()
  })
})

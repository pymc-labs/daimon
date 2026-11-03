import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import type { Agent } from "../types"
import { ToolRulesPanel } from "./ToolRulesPanel"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), put: vi.fn() } }))

const agent = {
  id: "ag_w", can_view_details: true,
  mcp_servers: [{ key: "notes", name: "Notes", tools: [{ name: "write" }] }],
} as unknown as Agent
const props = { node: "tools", agent, config: agent, ro: false, changed: new Set<string>(), update: () => {}, select: () => {} }

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("ToolRulesPanel", () => {
  it("changes a rule's scope with the revision it read", async () => {
    vi.mocked(api.get).mockResolvedValue({ active: true, rules: [{ tool: "notes/write", rule: "ask", scope: "chat", revision: 2 }] } as never)
    vi.mocked(api.put).mockResolvedValue({} as never)
    render(<QueryClientProvider client={new QueryClient()}><ToolRulesPanel {...props} /></QueryClientProvider>)
    await userEvent.click((await screen.findAllByRole("combobox", { name: "Applies to" }))[0]!)
    await userEvent.click(await screen.findByRole("option", { name: "Chat and routines" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/agents/ag_w/tool-rules", { tool: "notes/write", rule: "ask", scope: "chat_and_routines", expected_revision: 2 }))
  })

  it("only reads while tool safety is off", async () => {
    vi.mocked(api.get).mockResolvedValue({ active: false, rules: [{ tool: "notes/write", rule: "deny", scope: "chat", revision: 1 }] } as never)
    render(<QueryClientProvider client={new QueryClient()}><ToolRulesPanel {...props} /></QueryClientProvider>)
    expect(await screen.findByText("Read only")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Clear" })).toBeDisabled()
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull()
  })
})

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import { Mcp } from "./Mcp"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }))

const servers = [
  { id: "s1", name: "Linear", url: "https://mcp.linear.app/sse", signed_in: 3, agents: [{ id: "a1", name: "Daimon", shared: false, signed_in: 3 }] },
  { id: "s2", name: "Warehouse", url: "https://wh.example/mcp", signed_in: 0, agents: [{ id: "a2", name: "MMM Analyst", shared: true, signed_in: 0 }] },
]

describe("Mcp", () => {
  it("lists servers at once and names expired sign-ins when the check answers", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      (path.endsWith("/expiry") ? { complete: true, servers: [{ id: "s1", expired: 2, people: ["Ada", "Bo"] }] } : { servers }) as never)
    render(<QueryClientProvider client={new QueryClient()}><Mcp /></QueryClientProvider>)
    expect(await screen.findByText("Warehouse")).toBeInTheDocument()
    expect(screen.getByText("shared sign-in")).toBeInTheDocument()
    expect(await screen.findByText("2 sign-ins expired")).toBeInTheDocument()
    expect(screen.getByText("All current")).toBeInTheDocument()
  })

  it("keeps the list when the expiry check fails", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path.endsWith("/expiry")) throw new Error("down")
      return { servers } as never
    })
    render(<QueryClientProvider client={new QueryClient()}><Mcp /></QueryClientProvider>)
    expect(await screen.findByText("Linear")).toBeInTheDocument()
    expect(screen.queryByText(/expired/)).not.toBeInTheDocument()
  })
})

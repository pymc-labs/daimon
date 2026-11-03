import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import { Feedback } from "./Feedback"
import type { Feedback as Data } from "./types"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }))

const data: Data = {
  days: 30,
  agents: [{ id: "ag_a", name: "Daimon", up: 8, down: 3, reasons: [{ code: "too_slow", label: "Too slow", count: 2 }] }],
}

beforeEach(() => vi.clearAllMocks())

describe("Feedback", () => {
  it("counts thumbs and reasons per agent over the chosen period", async () => {
    vi.mocked(api.get).mockResolvedValue(data as never)
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><Feedback /></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByText("Daimon")).toBeInTheDocument()
    expect(screen.getByText("Too slow 2")).toBeInTheDocument()
    expect(api.get).toHaveBeenCalledWith("/api/admin/feedback?days=30")
  })

  it("says so when nobody voted", async () => {
    vi.mocked(api.get).mockResolvedValue({ days: 30, agents: [] } as never)
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><Feedback /></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByText("No feedback in this period")).toBeInTheDocument()
  })
})

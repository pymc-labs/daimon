import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import type { Agent } from "../types"
import { HelpersEditor } from "./HelpersEditor"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), put: vi.fn() } }))

const helpers = [{ id: "ag_a", name: "Argon" }, { id: "ag_b", name: "Boron" }]
const seeded = (canEditHelpers: boolean) => ({ id: "ag_sys", name: "Daimon", owner: { name: "System" }, helpers, can_edit_helpers: canEditHelpers }) as unknown as Agent
const props = (agent: Agent, update = vi.fn()) => ({ node: "helpers", agent, config: agent, ro: true, changed: new Set<string>(), update, select: () => {} })

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = () => {}
  vi.mocked(api.get).mockResolvedValue([{ id: "ag_sys", name: "Daimon" }, ...helpers] as never)
})

const show = (p: ReturnType<typeof props>) => render(<QueryClientProvider client={new QueryClient()}><HelpersEditor {...p} /></QueryClientProvider>)

describe("HelpersEditor on a seeded agent", () => {
  it("lets an admin remove a helper in place, without a draft", async () => {
    vi.mocked(api.put).mockResolvedValue(seeded(true) as never)
    const p = props(seeded(true))
    show(p)
    await userEvent.click(screen.getByRole("button", { name: "Remove Argon" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/agents/ag_sys/helpers", { helpers: ["ag_b"] }))
    expect(p.update).not.toHaveBeenCalled()
  })

  it("only reads for anyone else", () => {
    show(props(seeded(false)))
    expect(screen.getByText("Argon")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Remove Argon" })).toBeNull()
    expect(screen.queryByText("Add helper")).toBeNull()
  })
})

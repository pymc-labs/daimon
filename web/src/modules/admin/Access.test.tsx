import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { Access, Providers } from "./Access"
import type { Access as AccessState, Providers as ProvidersState } from "./types"

const open: AccessState = { people: null, others: 0, dm_enabled: false, dm_memory_read_only: false, plain_text_mentions: false }
const wrap = (ui: React.ReactNode) => render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>)

describe("Access", () => {
  it("sends each switch as its own change", async () => {
    const save = vi.fn()
    wrap(<Access access={open} save={save} />)
    await userEvent.click(screen.getByRole("switch", { name: "Typed mentions" }))
    expect(save).toHaveBeenCalledWith({ plain_text_mentions: true })
  })

  it("leaves out settings the chat platform lacks", () => {
    wrap(<Access access={{ ...open, dm_enabled: null, dm_memory_read_only: null }} save={vi.fn()} />)
    expect(screen.queryByText("Direct messages")).not.toBeInTheDocument()
    expect(screen.getByText("Typed mentions")).toBeInTheDocument()
  })

  it("names the chosen people and removes one", async () => {
    const save = vi.fn()
    const people = [{ id: "p1", name: "Maya" }, { id: "p2", name: "Jonas" }]
    wrap(<Access access={{ ...open, people, others: 2 }} save={save} />)
    expect(screen.getByText("2 more from chat")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Remove Maya" }))
    expect(save).toHaveBeenCalledWith({ invokers: { everyone: false, people: ["p2"] } })
  })

  it("will not remove the last person when nobody else is listed", async () => {
    const save = vi.fn()
    wrap(<Access access={{ ...open, people: [{ id: "p1", name: "Maya" }] }} save={save} />)
    await userEvent.click(screen.getByRole("button", { name: "Remove Maya" }))
    expect(save).not.toHaveBeenCalled()
  })
})

describe("Providers", () => {
  const providers: ProvidersState = {
    offered: [{ id: "anthropic", label: "Anthropic" }, { id: "openai", label: "OpenAI" }],
    allowed: ["anthropic"],
    followed: false,
  }

  it("adds a provider to the list, keeping Anthropic", async () => {
    const save = vi.fn()
    wrap(<Providers providers={providers} save={save} />)
    await userEvent.click(screen.getByRole("switch", { name: "OpenAI" }))
    expect(save).toHaveBeenCalledWith({ providers: ["anthropic", "openai"] })
  })

  it("locks the switches while the list follows the defaults", () => {
    wrap(<Providers providers={{ ...providers, followed: true }} save={vi.fn()} />)
    expect(screen.getByRole("switch", { name: "OpenAI" })).toBeDisabled()
  })
})

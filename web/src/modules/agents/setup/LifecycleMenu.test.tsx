import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { api } from "@/lib/api"
import type { Agent } from "../types"
import { LifecycleMenu } from "./LifecycleMenu"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn() } }))

const agent = { id: "ag_w", name: "Helper" } as Agent

const view = (role: string) => {
  vi.mocked(api.get).mockResolvedValue({ role, capabilities: [] } as never)
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter><ConfirmProvider><LifecycleMenu agent={agent} /></ConfirmProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = () => {}
  Element.prototype.hasPointerCapture = () => false
})

describe("LifecycleMenu", () => {
  it("shows a member nothing", async () => {
    view("member")
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(screen.queryByRole("button", { name: "More" })).not.toBeInTheDocument()
  })

  it("archives only after a confirm", async () => {
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    view("admin")
    await userEvent.click(await screen.findByRole("button", { name: "More" }))
    await userEvent.click(await screen.findByRole("menuitem", { name: "Archive" }))
    expect(api.post).not.toHaveBeenCalled()
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Archive" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/agents/ag_w/archive"))
  })

  it("copies under the name given", async () => {
    vi.mocked(api.post).mockResolvedValue({ id: "ag_new", name: "Twin" } as never)
    view("admin")
    await userEvent.click(await screen.findByRole("button", { name: "More" }))
    await userEvent.click(await screen.findByRole("menuitem", { name: "Copy" }))
    const input = await screen.findByRole("textbox", { name: "Name" })
    await userEvent.clear(input)
    await userEvent.type(input, "Twin")
    await userEvent.click(screen.getByRole("button", { name: "Copy" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/agents/ag_w/copy", { name: "Twin" }))
  })
})

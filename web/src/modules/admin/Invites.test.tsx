import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import { renderAt } from "@/test/render"
import { NewInvite } from "./Invites"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn() },
}))

const agents = [{ id: "ag1", name: "Pricing Analyst", avatar: "", model: "Opus" }]

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  vi.mocked(api.get).mockResolvedValue(agents as never)
  vi.mocked(api.post).mockResolvedValue({ id: "i1", email: "a@b.example", link: "https://x.example/i", expires_at: "2026-03-10T11:00:00Z" } as never)
  renderAt("/?dialog=invite", "/", <NewInvite />)
})

describe("NewInvite files", () => {
  it("offers an Allow files switch to visitors only, off by default", async () => {
    const files = await screen.findByRole("switch", { name: "Allow files" })
    expect(files).not.toBeChecked()
    await userEvent.click(screen.getByRole("radio", { name: "Member" }))
    expect(screen.queryByRole("switch", { name: "Allow files" })).not.toBeInTheDocument()
  })

  it("sends the choice with the invite", async () => {
    await userEvent.type(await screen.findByPlaceholderText("name@company.com"), "a@b.example")
    await userEvent.click(await screen.findByRole("checkbox"))
    await userEvent.click(screen.getByRole("switch", { name: "Allow files" }))
    await userEvent.click(screen.getByRole("button", { name: "Create link" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/admin/invites", expect.objectContaining({ role: "visitor", allow_files: true })))
  })

  it("never asks a member invite for files", async () => {
    await userEvent.type(await screen.findByPlaceholderText("name@company.com"), "a@b.example")
    await userEvent.click(screen.getByRole("switch", { name: "Allow files" }))
    await userEvent.click(screen.getByRole("radio", { name: "Member" }))
    await userEvent.click(screen.getByRole("button", { name: "Create link" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/admin/invites", expect.objectContaining({ role: "member", allow_files: false })))
  })
})

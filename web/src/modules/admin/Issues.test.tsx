import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { issueDetailOf, issuesOf } from "@/mock/ops"
import { renderAt } from "@/test/render"
import { Where } from "@/test/Where"
import { Issues } from "./Issues"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), page: vi.fn(), post: vi.fn() },
}))

const view = (url: string) => renderAt(url, "/admin/issues", <TooltipProvider><Issues /><Where /></TooltipProvider>)
const listCalls = () => vi.mocked(api.page).mock.calls.map(([p]) => p).filter((p) => p.startsWith("/api/admin/issues?"))

beforeEach(() => {
  vi.mocked(api.get).mockImplementation(async (path: string) =>
    path === "/api/me" ? { capabilities: ["audit.view", "workspace.manage", "routines.view"] }
      : path === "/api/agents" ? [] : issueDetailOf(decodeURIComponent(path.split("/").pop()!)))
  vi.mocked(api.page).mockImplementation(async (path: string) => ({ rows: path.includes("/events") ? [] : issuesOf(new URL(path, "http://x").searchParams), next: null }))
})

describe("Issues", () => {
  it("keeps a picked filter in the address and asks the server for it", async () => {
    view("/admin/issues")
    await screen.findByText("Routine failed to run")
    await userEvent.click(screen.getByRole("radio", { name: "7 days" }))
    expect(screen.getByTestId("where")).toHaveTextContent("/admin/issues?period=7d")
    await waitFor(() => expect(listCalls().at(-1)).toBe("/api/admin/issues?period=7d"))
  })

  it("opens the issue named in the address in a drawer with its actions", async () => {
    view("/admin/issues?issue=f_fire_r2")
    expect(await screen.findByRole("link", { name: "Open routine" })).toHaveAttribute("href", "/routines?routine=r2")
    expect(await screen.findByRole("button", { name: "Resolve" })).toBeInTheDocument()
    expect(vi.mocked(api.get)).toHaveBeenCalledWith("/api/admin/issues/f_fire_r2")
  })

  it("copies the issue link from a drawer whose subject has no page to open", async () => {
    const user = userEvent.setup()
    const copy = vi.spyOn(navigator.clipboard, "writeText")
    view("/admin/issues?issue=f_watch_inbox")
    await screen.findByRole("button", { name: "Resolve" })
    expect(screen.queryByRole("link", { name: /^Open (routine|agent)$/ })).toBeNull()
    await user.click(screen.getAllByRole("button", { name: "Copy link" }).at(-1)!)
    expect(copy).toHaveBeenCalledTimes(1)
  })
})

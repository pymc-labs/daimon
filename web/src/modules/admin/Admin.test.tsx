import { screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { newClient, renderAt } from "@/test/render"
import { Where } from "@/test/Where"
import { Admin } from "./Admin"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), page: vi.fn() },
}))

describe("Admin", () => {
  it("sends a person link from before People moved to the People tab", async () => {
    Element.prototype.scrollIntoView = vi.fn()
    const me = { capabilities: ["people.manage", "audit.view"], workspace: { name: "Workspace" } }
    vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/me" ? me : null))
    const client = newClient()
    client.setQueryData(["me"], me) // the shell loads it before any page
    vi.mocked(api.page).mockResolvedValue({ rows: [], next: null })
    renderAt("/admin?person=p1", "/admin/*", <TooltipProvider><Admin /><Where /></TooltipProvider>, client)
    expect(await screen.findByTestId("where")).toHaveTextContent("/admin/people?person=p1")
  })
})

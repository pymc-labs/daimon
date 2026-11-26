import { screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { overviewOf } from "@/mock/ops"
import { renderAt } from "@/test/render"
import { Overview } from "./Overview"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), page: vi.fn() },
}))

describe("Overview", () => {
  it("shows the tiles of the overview the server sends, each opening its page", async () => {
    const data = overviewOf("24h")
    vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/me" ? { capabilities: ["audit.view"] } : data))
    renderAt("/admin", "/admin", <TooltipProvider><Overview /></TooltipProvider>)
    expect(await screen.findByText("Degraded")).toBeInTheDocument()
    expect(vi.mocked(api.get)).toHaveBeenCalledWith("/api/admin/overview?period=24h")
    expect(screen.getByText(data.answers.toLocaleString("en"))).toBeInTheDocument()
    expect(screen.getByText("1 of 3")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: /Failed answers/ })).toHaveAttribute("href", "/admin/issues?area=turn")
    expect(screen.getByRole("link", { name: /Routines failing/ })).toHaveAttribute("href", "/routines?status=failed")
    expect(screen.getByRole("link", { name: /Credits left.*\$1,240\.50/ })).toHaveAttribute("href", "/credits")
    expect(screen.getByText(data.attention[0]!.title, { selector: "span" })).toBeInTheDocument()
  })
})

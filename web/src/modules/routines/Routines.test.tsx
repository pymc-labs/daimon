import { screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import { renderAt } from "@/test/render"
import { Routines } from "./Routines"

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn() },
}))

describe("Routines", () => {
  it("labels the page Legacy and links the workflows page", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/me" ? { capabilities: ["routines.view", "workflows.view"], workspace: { name: "Acme" } } : []))
    renderAt("/routines", "/routines", <Routines />)
    expect(await screen.findByRole("link", { name: "Legacy, use Workflows" })).toHaveAttribute("href", "/workflows")
  })
})

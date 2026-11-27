import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import type { Legal } from "@/lib/types"
import { SignIn } from "./SignIn"

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), post: vi.fn() } }))

const PRIVACY = "https://example.com/privacy"

const show = async (legal: Legal) => {
  vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/auth/legal" ? legal : [{ id: "discord", label: "Discord", kind: "redirect" }]) as never)
  render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><SignIn /></MemoryRouter></QueryClientProvider>)
  return screen.findByRole("link", { name: "Privacy" })
}

beforeEach(() => vi.clearAllMocks())

describe("SignIn legal links", () => {
  it("links the privacy policy and the terms", async () => {
    expect((await show({ privacy: PRIVACY, terms: "https://example.com/terms" })).getAttribute("href")).toBe(PRIVACY)
    expect(screen.getByRole("link", { name: "Terms" }).getAttribute("href")).toBe("https://example.com/terms")
  })

  it("leaves out Terms until the operator sets one", async () => {
    await show({ privacy: PRIVACY, terms: null })
    expect(screen.queryByRole("link", { name: "Terms" })).toBeNull()
  })
})

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import { Redeem } from "./Redeem"

vi.mock("@/lib/api", () => ({ api: { post: vi.fn() } }))

const view = (onOpenChange = vi.fn()) =>
  render(<QueryClientProvider client={new QueryClient()}><Redeem open onOpenChange={onOpenChange} /></QueryClientProvider>)

describe("Redeem", () => {
  it("keeps the button off until a code is typed", async () => {
    view()
    const button = screen.getByRole("button", { name: "Redeem" })
    expect(button).toBeDisabled()
    await userEvent.type(screen.getByLabelText("Code"), "spring")
    expect(button).toBeEnabled()
  })

  it("sends the trimmed code and closes on success", async () => {
    vi.mocked(api.post).mockResolvedValue({ balance_usd: 125, amount_usd: 25, starts_at: null })
    const onOpenChange = vi.fn()
    view(onOpenChange)
    await userEvent.type(screen.getByLabelText("Code"), "  SPRING-2026 ")
    await userEvent.click(screen.getByRole("button", { name: "Redeem" }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(api.post).toHaveBeenCalledWith("/api/credits/redeem", { code: "SPRING-2026" })
  })
})

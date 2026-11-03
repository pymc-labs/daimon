import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import { RunNow } from "./RunNow"
import type { Routine } from "./types"

vi.mock("@/lib/api", () => ({ api: { post: vi.fn() } }))

const routine: Routine = {
  id: "r1", agent: { id: "a1", name: "Analyst" }, cron: "0 9 * * *", timezone: "UTC", schedule: "every day at 09:00 UTC", message: "report",
  enabled: true, catch_up: "skip", destination: null, next_run: "2030-01-01T09:00:00Z", last_run: null, failed: false, last_output: null, mine: true, owner: null,
}
const view = (r: Routine) => render(<QueryClientProvider client={new QueryClient()}><RunNow routine={r} /></QueryClientProvider>)

describe("RunNow", () => {
  it("asks the server to run the routine", async () => {
    vi.mocked(api.post).mockResolvedValue(routine)
    view(routine)
    await userEvent.click(screen.getByRole("button", { name: "Run now" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/routines/r1/run"))
  })

  it("is off while the routine is paused", () => {
    view({ ...routine, enabled: false, next_run: null })
    expect(screen.getByRole("button", { name: "Run now" })).toBeDisabled()
  })
})

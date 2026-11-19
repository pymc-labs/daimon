import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { toast } from "sonner"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import { ThreadModel } from "./ThreadModel"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), put: vi.fn() } }))
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const models = {
  current: null,
  choices: [
    { value: "anthropic:claude-haiku-5-5", label: "Haiku 5.5", price: "$1 / $5", provider: "anthropic" },
    { value: "openai:gpt-6-luna", label: "GPT-6 Luna", price: "$2 / $10", provider: "openai" },
  ],
}
const open = async (onChanged = vi.fn()) => {
  render(<QueryClientProvider client={new QueryClient()}><ThreadModel sid="s1" model="Sonnet 5.5" onChanged={onChanged} /></QueryClientProvider>)
  await userEvent.click(screen.getByRole("button", { name: "Chat model" }))
  return onChanged
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.get).mockResolvedValue(models as never)
  vi.mocked(api.put).mockResolvedValue({ note: "This conversation runs on GPT-6 Luna from the next reply. Memory is off there." } as never)
})

describe("ThreadModel", () => {
  it("lists the catalog with prices", async () => {
    await open()
    expect(await screen.findByText("GPT-6 Luna")).toBeInTheDocument()
    expect(screen.getByText("$2 / $10")).toBeInTheDocument()
  })

  it("keeps a pick on the agent's own provider at once", async () => {
    const changed = await open()
    await userEvent.click(await screen.findByText("Haiku 5.5"))
    await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/chat/sessions/s1/model", { value: "anthropic:claude-haiku-5-5" }))
    expect(changed, "the chat refreshes").toHaveBeenCalled()
  })

  it("keeps a cross-provider pick at once and says what is off as information", async () => {
    await open()
    await userEvent.click(await screen.findByText("GPT-6 Luna"))
    await vi.waitFor(() => expect(api.put, "no card first").toHaveBeenCalledWith("/api/chat/sessions/s1/model", { value: "openai:gpt-6-luna" }))
    expect(api.get, "no preview fetched").toHaveBeenCalledTimes(1)
    expect(toast.success, "the note names what is off").toHaveBeenCalledWith(expect.stringContaining("Memory is off there."))
    expect(screen.queryByRole("button", { name: "Switch" }), "no switch card").not.toBeInTheDocument()
  })

  it("says why a refused pick was not kept", async () => {
    vi.mocked(api.put).mockRejectedValue(new Error("Ask an admin."))
    await open()
    await userEvent.click(await screen.findByText("GPT-6 Luna"))
    await vi.waitFor(() => expect(toast.error, "the refusal line").toHaveBeenCalledWith("Ask an admin."))
  })
})

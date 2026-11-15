import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
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
const stays = { moves: false, allowed: true, skills: [], servers: [], capabilities: [], shared_limits: [], drops: [], cost: "", threads: "", digest: "", revision: 0, refusal: null }
const moves = { ...stays, moves: true, drops: ["memory"], cost: "Costs about the same", threads: "This chat only.", digest: "d1", revision: 4 }

const open = async (onChanged = vi.fn()) => {
  render(<QueryClientProvider client={new QueryClient()}><ThreadModel sid="s1" model="Sonnet 5.5" onChanged={onChanged} /></QueryClientProvider>)
  await userEvent.click(screen.getByRole("button", { name: "Chat model" }))
  return onChanged
}
const previews = (preview: unknown) =>
  vi.mocked(api.get).mockImplementation(async (path: string) => (path.endsWith("/models") ? models : preview) as never)

beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.put).mockResolvedValue({ note: "Done." } as never) })

describe("ThreadModel", () => {
  it("lists the catalog with prices", async () => {
    previews(stays)
    await open()
    expect(await screen.findByText("GPT-6 Luna")).toBeInTheDocument()
    expect(screen.getByText("$2 / $10")).toBeInTheDocument()
  })

  it("keeps a pick on the agent's own provider at once", async () => {
    previews(stays)
    const changed = await open()
    await userEvent.click(await screen.findByText("Haiku 5.5"))
    await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/chat/sessions/s1/model", { value: "anthropic:claude-haiku-5-5", preview_digest: "", preview_revision: 0 }))
    expect(changed).toHaveBeenCalled()
  })

  it("shows what a cross-provider pick changes and keeps it only on Switch", async () => {
    previews(moves)
    await open()
    await userEvent.click(await screen.findByText("GPT-6 Luna"))
    expect(await screen.findByText("Memory")).toBeInTheDocument()
    expect(api.put).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole("button", { name: "Switch" }))
    await vi.waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/chat/sessions/s1/model", { value: "openai:gpt-6-luna", preview_digest: "d1", preview_revision: 4 }))
  })

  it("offers no Switch for a refused pick", async () => {
    previews({ ...moves, allowed: false, refusal: { line: "Ask an admin.", action: null } })
    await open()
    await userEvent.click(await screen.findByText("GPT-6 Luna"))
    expect(await screen.findByText("Ask an admin.")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Switch" })).toBeDisabled()
  })
})

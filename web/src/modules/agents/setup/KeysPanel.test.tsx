import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { Agent } from "../types"
import { CodingPanel } from "./CodingPanel"
import { KeysPanel } from "./KeysPanel"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const agent = { id: "ag_w", can_view_details: true } as Agent
const props = { node: "keys", agent, config: agent, ro: false, changed: new Set<string>(), update: () => {}, select: () => {} }

const view = (ui: React.ReactNode) =>
  render(<QueryClientProvider client={new QueryClient()}><TooltipProvider><ConfirmProvider>{ui}</ConfirmProvider></TooltipProvider></QueryClientProvider>)

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = () => {}
  Element.prototype.hasPointerCapture = () => false
})

describe("KeysPanel", () => {
  it("lists names and takes the value in a masked field", async () => {
    vi.mocked(api.get).mockResolvedValue([{ key: "SERVICE_API_KEY", updated_at: "2026-11-01T00:00:00Z" }] as never)
    view(<KeysPanel {...props} />)
    expect(await screen.findByText("SERVICE_API_KEY")).toBeInTheDocument()
    expect(screen.getByLabelText("Value")).toHaveAttribute("type", "password")
  })

  it("asks first, then sends the value once and clears the form", async () => {
    vi.mocked(api.get).mockResolvedValue([] as never)
    vi.mocked(api.put).mockResolvedValue({ key: "NEW_API_KEY", updated_at: "2026-11-01T00:00:00Z" } as never)
    view(<KeysPanel {...props} />)
    await userEvent.type(screen.getByLabelText("Name"), "NEW_API_KEY")
    await userEvent.type(screen.getByLabelText("Value"), "hunter2")
    await userEvent.click(screen.getByRole("button", { name: "Add" }))
    expect(api.put).not.toHaveBeenCalled()
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Add" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/agents/ag_w/keys/NEW_API_KEY", { value: "hunter2" }))
    await waitFor(() => expect(screen.getByLabelText("Value")).toHaveValue(""))
  })

  it("removes only after a confirm", async () => {
    vi.mocked(api.get).mockResolvedValue([{ key: "SERVICE_API_KEY", updated_at: "2026-11-01T00:00:00Z" }] as never)
    vi.mocked(api.del).mockResolvedValue({ removed: true } as never)
    view(<KeysPanel {...props} />)
    await userEvent.click(await screen.findByRole("button", { name: "Remove SERVICE_API_KEY" }))
    expect(api.del).not.toHaveBeenCalled()
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Remove" }))
    await waitFor(() => expect(api.del).toHaveBeenCalledWith("/api/agents/ag_w/keys/SERVICE_API_KEY"))
  })
})

/** A batch that ends `ttlMs` from now. */
const batch = (n: number, ttlMs = 10 * 60_000) => ({
  batch: `b${n}`, expires_at: new Date(Date.now() + ttlMs).toISOString(),
  prompts: [{ id: "claude-code", name: "Claude Code", prompt: `claude prompt ${n}` }, { id: "codex", name: "Codex", prompt: `codex prompt ${n}` }],
})

/** Routes the panel's calls: keys and connect status. */
function serve(connected = false) {
  vi.mocked(api.get).mockImplementation(async (url: string) => {
    if (url.includes("/connect/")) return (connected ? { connected: true, client: "codex" } : { connected: false, client: null }) as never
    return [{ id: "k9", creator: "Maya", created_at: "2026-11-01T00:00:00Z", expires_at: null, mine: true, client: "codex" }] as never
  })
}

/** Picks a tool in the Tool select. */
async function pick(name: string) {
  await userEvent.click(screen.getByRole("combobox", { name: "Tool" }))
  await userEvent.click(await screen.findByRole("option", { name }))
}

describe("CodingPanel", () => {
  it("shows the Claude Code prompt first and copies it", async () => {
    serve()
    vi.mocked(api.post).mockImplementation(async () => batch(1) as never)
    view(<CodingPanel {...props} node="coding" />)
    expect(await screen.findByLabelText("Prompt"), "the first tool's prompt shows").toHaveTextContent("claude prompt 1")
    expect(screen.getByRole("button", { name: "Copy prompt" })).toBeInTheDocument()
    expect(screen.getByText("Waiting"), "an unused prompt reads as waiting").toBeInTheDocument()
    expect(api.post).toHaveBeenCalledWith("/api/agents/ag_w/coding-keys/connect", {})
  })

  it("switching tools shows that tool's prompt and mints nothing", async () => {
    serve()
    vi.mocked(api.post).mockImplementation(async () => batch(1) as never)
    view(<CodingPanel {...props} node="coding" />)
    await screen.findByLabelText("Prompt")
    await pick("Codex")
    expect(screen.getByLabelText("Prompt"), "the Codex prompt of the same batch").toHaveTextContent("codex prompt 1")
    await pick("Token")
    await pick("Claude Code")
    expect(screen.getByLabelText("Prompt")).toHaveTextContent("claude prompt 1")
    expect(api.post, "one mint for the whole visit").toHaveBeenCalledTimes(1)
  })

  it("lists each key with its client", async () => {
    serve()
    vi.mocked(api.post).mockResolvedValue(batch(1) as never)
    view(<CodingPanel {...props} node="coding" />)
    const row = (await screen.findByText("Maya")).closest("div.flex.items-center.gap-2") as HTMLElement
    expect(within(row).getByText("Codex"), "the key row names its client").toBeInTheDocument()
  })

  it("shows connected once the code is used, and connects another with a fresh batch", async () => {
    serve(true)
    let n = 0
    vi.mocked(api.post).mockImplementation(async () => batch(++n) as never)
    view(<CodingPanel {...props} node="coding" />)
    expect(await screen.findByText("Connected")).toBeInTheDocument()
    expect(screen.queryByLabelText("Prompt"), "a used prompt is gone").toBeNull()
    await userEvent.click(screen.getByRole("button", { name: "Connect another" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2))
    expect(api.post, "a used batch has nothing to replace").toHaveBeenLastCalledWith("/api/agents/ag_w/coding-keys/connect", {})
  })

  it("never mints on a timer; once the batch ends it says Expired and New prompt replaces it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      serve()
      let n = 0
      vi.mocked(api.post).mockImplementation(async () => batch(++n, 60_000) as never)
      view(<CodingPanel {...props} node="coding" />)
      expect(await screen.findByLabelText("Prompt")).toHaveTextContent("claude prompt 1")
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      expect(api.post, "no re-mint before the batch ends").toHaveBeenCalledTimes(1)
      await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
      expect(await screen.findByText("Expired")).toBeInTheDocument()
      expect(screen.queryByLabelText("Prompt"), "an expired prompt is not shown").toBeNull()
      expect(api.post, "expiry alone mints nothing").toHaveBeenCalledTimes(1)
      await userEvent.click(screen.getByRole("button", { name: "New prompt" }))
      await waitFor(() => expect(screen.getByLabelText("Prompt")).toHaveTextContent("claude prompt 2"))
      expect(api.post).toHaveBeenLastCalledWith("/api/agents/ag_w/coding-keys/connect", { replaces: "b1" })
      expect(screen.getByText("Waiting")).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it("shows the error, not a stale prompt, when a new prompt fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      serve()
      vi.mocked(api.post).mockResolvedValueOnce(batch(1, 60_000) as never).mockRejectedValueOnce(new Error("Mint failed"))
      view(<CodingPanel {...props} node="coding" />)
      await screen.findByLabelText("Prompt")
      await act(async () => { await vi.advanceTimersByTimeAsync(61_000) })
      await userEvent.click(await screen.findByRole("button", { name: "New prompt" }))
      expect(await screen.findByText("Mint failed")).toBeInTheDocument()
      expect(screen.queryByLabelText("Prompt"), "the failed mint leaves no old prompt").toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it("shows the error when the first mint fails", async () => {
    serve()
    vi.mocked(api.post).mockRejectedValue(new Error("No access"))
    view(<CodingPanel {...props} node="coding" />)
    expect(await screen.findByText("No access")).toBeInTheDocument()
    expect(screen.queryByLabelText("Prompt")).toBeNull()
  })

  it("shows a new key's URL and token once, and not again after Done", async () => {
    serve()
    vi.mocked(api.post).mockImplementation(async (url: string) => (
      url.endsWith("/connect") ? batch(1) : { id: "k1", token: "tok-shown-once", url: "https://x.example/mcp", expires_at: "2026-12-01T00:00:00Z" }
    ) as never)
    view(<CodingPanel {...props} node="coding" />)
    await screen.findByLabelText("Prompt")
    await pick("Token")
    await userEvent.click(await screen.findByRole("button", { name: "Make key" }))
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Make key" }))
    expect(await screen.findByText(/tok-shown-once/)).toBeInTheDocument()
    expect(screen.getByText("https://x.example/mcp")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Done" }))
    await waitFor(() => expect(screen.queryByText(/tok-shown-once/)).not.toBeInTheDocument())
  })
})

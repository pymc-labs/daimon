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

const batch = (n: number) => ({
  batch: `b${n}`, expires_at: "2026-11-05T12:10:00Z",
  prompts: [{ id: "claude-code", name: "Claude Code", prompt: `claude prompt ${n}` }, { id: "codex", name: "Codex", prompt: `codex prompt ${n}` }],
})

/** Routes the panel's calls: keys, connect status and the mint calls it makes. */
function serve(connected = false) {
  vi.mocked(api.get).mockImplementation(async (url: string) => {
    if (url.includes("/connect/")) return (connected ? { connected: true, client: "codex" } : { connected: false, client: null }) as never
    return [{ id: "k9", creator: "Maya", created_at: "2026-11-01T00:00:00Z", expires_at: null, mine: true, client: "codex" }] as never
  })
}

describe("CodingPanel", () => {
  it("shows the Claude Code prompt first and copies it", async () => {
    serve()
    let n = 0
    vi.mocked(api.post).mockImplementation(async () => batch(++n) as never)
    view(<CodingPanel {...props} node="coding" />)
    expect(await screen.findByLabelText("Prompt")).toHaveTextContent("claude prompt 1")
    expect(screen.getByRole("button", { name: "Copy prompt" })).toBeInTheDocument()
    expect(screen.getByText("Waiting")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("radio", { name: "Codex" }))
    expect(screen.getByLabelText("Prompt")).toHaveTextContent("codex prompt 1")
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledWith("/api/agents/ag_w/coding-keys/connect", {})
  })

  it("lists each key with its client", async () => {
    serve()
    vi.mocked(api.post).mockResolvedValue(batch(1) as never)
    view(<CodingPanel {...props} node="coding" />)
    const row = (await screen.findByText("Maya")).closest("div.flex.items-center.gap-2") as HTMLElement
    expect(within(row).getByText("Codex")).toBeInTheDocument()
  })

  it("shows connected once the code is used", async () => {
    serve(true)
    vi.mocked(api.post).mockResolvedValue(batch(1) as never)
    view(<CodingPanel {...props} node="coding" />)
    expect(await screen.findByText("Connected")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Connect another" })).toBeInTheDocument()
  })

  it("mints a new batch about every 9 minutes, replacing the last", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      serve()
      let n = 0
      vi.mocked(api.post).mockImplementation(async () => batch(++n) as never)
      view(<CodingPanel {...props} node="coding" />)
      expect(await screen.findByLabelText("Prompt")).toHaveTextContent("claude prompt 1")
      await act(async () => { await vi.advanceTimersByTimeAsync(9 * 60_000 + 100) })
      await waitFor(() => expect(screen.getByLabelText("Prompt")).toHaveTextContent("claude prompt 2"))
      expect(api.post).toHaveBeenLastCalledWith("/api/agents/ag_w/coding-keys/connect", { replaces: "b1" })
    } finally {
      vi.useRealTimers()
    }
  })

  it("shows a new key's URL and token once, and not again after Done", async () => {
    serve()
    vi.mocked(api.post).mockImplementation(async (url: string) => (
      url.endsWith("/connect") ? batch(1) : { id: "k1", token: "tok-shown-once", url: "https://x.example/mcp", expires_at: "2026-12-01T00:00:00Z" }
    ) as never)
    view(<CodingPanel {...props} node="coding" />)
    await userEvent.click(await screen.findByRole("radio", { name: "Token" }))
    await userEvent.click(await screen.findByRole("button", { name: "Make key" }))
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Make key" }))
    expect(await screen.findByText(/tok-shown-once/)).toBeInTheDocument()
    expect(screen.getByText("https://x.example/mcp")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Done" }))
    await waitFor(() => expect(screen.queryByText(/tok-shown-once/)).not.toBeInTheDocument())
  })
})

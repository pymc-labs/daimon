import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { Capability, Me } from "@/lib/types"
import { renderAt } from "@/test/render"
import { sidebarFor } from "../registry"
import { Compare } from "./Compare"
import { NewRun } from "./NewRun"

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), post: vi.fn() } }))

const me = (modules: Me["modules"]): Me => ({
  id: "p1", name: "Ada", email: "ada@example.com", role: "member", workspace: { id: "w1", name: "QA", platform: "discord", role: "member", members: 3 }, workspaces: [],
  modules, capabilities: ["chat.use", "evals.use"] as Capability[], identities: [], accounts: [], credit: null,
})

const item = (arm: string) => ({ id: `r:c:${arm}`, run_id: "r", case: { id: "c", revision: 1 }, arm_id: arm, repeat: 0, status: "done", chat: [], outputs: [], world_calls: [] })
const suite = { id: "s", version: 1, name: "Suite", cases: [{ id: "c", revision: 1, hash: "h" }], defaults: { judges: [] } }
const cases = [{ id: "c", revision: 1, hash: "h", title: "Summarize spend", fidelity: "full", platform: "web", tags: [] }]
const arms = [{ id: "arm-x", label: "Arm X", kind: "live", live: { backend: "h", model: "m" } }]
const run = { id: "r", name: "Run", suite: { id: "s", version: 1 }, arms, repeats: 1, judges: [], origin: { kind: "local" }, status: "done" }

beforeEach(() => vi.resetAllMocks())

describe("evals module", () => {
  it("shows in the sidebar only when the module is on", () => {
    expect(sidebarFor(me(["chat", "evals"])).map((i) => i.to)).toContain("/evals")
    expect(sidebarFor(me(["chat"])).map((i) => i.to)).not.toContain("/evals")
  })

  it("compares blind with labels and no arm names", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/api/me") return me(["evals"]) as never
      if (path.includes("/compare")) return { run_id: "r", case_id: "c", repeat: 0, blind: true, columns: [{ label: "A", item: item("A"), scores: [] }, { label: "B", item: item("B"), scores: [] }] } as never
      if (path.includes("/items")) return [{ ...item("A"), key: "c:A:0", case_id: "c", arm_id: "A", case_revision: 1 }] as never
      if (path.includes("/cases/")) return { id: "c", surface: { platform: "web", participants: [], bot: { id: "bot", display_name: "Assistant" } } } as never
      if (path.includes("/cases")) return cases as never
      return run as never
    })
    renderAt("/evals/runs/r/compare?blind=1", "/evals/runs/:run/compare", <TooltipProvider><Compare /></TooltipProvider>)
    expect(await screen.findByText("A")).toBeInTheDocument()
    expect(screen.getByText("B")).toBeInTheDocument()
    expect(screen.queryByText("Arm X")).not.toBeInTheDocument()
  })

  it("starts a run only after a cap is set, and sends the cap", async () => {
    const lines = [{ category: "agent", items: 1, expected_usd: 1 }]
    vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/me" ? me(["evals"]) : path.includes("/arms") ? arms : path.includes("/cases") ? cases : suite) as never)
    vi.mocked(api.post).mockImplementation(async (path: string) => (path.includes("/estimate") ? { low_usd: 1, expected_usd: 2, high_usd: 4, at: "", lines } : { ...run, id: "r2" }) as never)
    renderAt("/evals/suites/s/run", "/evals/suites/:suite/run", <NewRun />)
    await userEvent.click(await screen.findByRole("checkbox", { name: /Arm X/ }))
    const go = await screen.findByRole("button", { name: "Approve and run" })
    expect(go).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/cap/i), "5")
    expect(go).toBeEnabled()
    await userEvent.click(go)
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/evals/runs", expect.objectContaining({ cap_usd: 5, arm_ids: ["arm-x"] })))
  })
})

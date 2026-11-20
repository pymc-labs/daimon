import { screen, waitFor } from "@testing-library/react"
import { setupServer } from "msw/node"
import userEvent from "@testing-library/user-event"
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { evalsHandlers } from "@/mock/evals"
import { session } from "@/mock/world"
import type { Capability, Me } from "@/lib/types"
import { renderAt } from "@/test/render"
import { sidebarFor } from "../registry"
import { Compare } from "./Compare"
import { NewRun } from "./NewRun"
import { Review } from "./Review"

vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), post: vi.fn() } }))

const me = (modules: Me["modules"]): Me => ({
  id: "p1", name: "Ada", email: "ada@example.com", role: "member", workspace: { id: "w1", name: "QA", platform: "discord", role: "member", members: 3 }, workspaces: [],
  modules, capabilities: ["chat.use", "evals.use"] as Capability[], identities: [], accounts: [], credit: null,
})

const item = (id: string) => ({ id, run_id: "run", case: { id: "c", revision: 1 }, arm_id: "Arm 1", repeat: 0, status: "done", chat: [], outputs: [], world_calls: [{ seq: 1, surface: "mcp", tool: "read", origin: "recorded" }] })
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

  it("compares blind with labels only: no arm, run name, cost or origin on screen", async () => {
    const col = (label: string, id: string) => ({ label, item_id: id, status: "done", passed: null, rubric: null, reply: "Hello", scores: [] })
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === "/api/me") return me(["evals"]) as never
      if (path.includes("/compare")) return { run_id: "run", blind: true, rows: [{ case_id: "c", title: "Summarize spend", repeat: 0, columns: [col("Arm 1", "i1"), col("Arm 2", "i2")] }] } as never
      if (path.includes("/cases/")) return { id: "c", surface: { platform: "web", participants: [], bot: { id: "bot", display_name: "Assistant" } } } as never
      if (path.includes("/items/")) return item(path.includes("i1") ? "i1" : "i2") as never
      if (path.endsWith("/suites")) return [{ id: "s", name: "Suite", version: 1, role: "viewer", scrub_status: "scrubbed" }] as never
      return run as never
    })
    renderAt("/evals/runs/r/compare?blind=1", "/evals/runs/:run/compare", <TooltipProvider><Compare /></TooltipProvider>)
    expect(await screen.findByText("Arm 1")).toBeInTheDocument()
    expect(screen.getByText("Arm 2")).toBeInTheDocument()
    expect(screen.queryByText("Arm X")).not.toBeInTheDocument()
    expect(screen.queryByText("Run")).not.toBeInTheDocument()
    expect(screen.getByText("Review")).toBeInTheDocument()
    expect(screen.queryByText("recorded")).not.toBeInTheDocument()
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument()
  })

  it("starts a run only after a cap is set, and sends the cap", async () => {
    const lines = [{ category: "agent", items: 1, expected_usd: 1 }]
    vi.mocked(api.get).mockImplementation(async (path: string) => (path === "/api/me" ? me(["evals"]) : path.endsWith("/suites") ? [{ id: "s", name: "Suite", version: 1, role: "owner", scrub_status: "scrubbed" }] : path.includes("/arms") ? arms : path.includes("/cases") ? cases : suite) as never)
    vi.mocked(api.post).mockImplementation(async (path: string) => (path.includes("/estimate") ? { low_usd: 1, expected_usd: 2, high_usd: 4, at: "", lines, admin_approval_usd: 25 } : { ...run, id: "r2" }) as never)
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

describe("review", () => {
  const task = {
    id: "r~q#c", queue: "r~q", layout: "side_by_side", blind: true, comments: [], reserved_until: null, done: 0, total: 3,
    case: { id: "c", title: "Summarize spend", platform: "web", history: [], turn: [], reference: null },
    criteria: [{ id: "answers", question: "Answers the question", kind: "binary" }, { id: "preference", question: "Which is better", kind: "preference" }],
    columns: [{ label: "A", item: item("i1") }, { label: "B", item: item("i2") }],
  }

  it("scores each column apart, names the column, and offers no skip", async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => (path.includes("/next") ? task : me(["evals"])) as never)
    vi.mocked(api.post).mockResolvedValue(undefined as never)
    renderAt("/evals/queues/r~q", "/evals/queues/:queue", <TooltipProvider><Review /></TooltipProvider>)
    await screen.findAllByText("Which is better")
    expect(screen.queryByRole("button", { name: /skip/i })).not.toBeInTheDocument()
    const pass = screen.getAllByRole("button", { name: /^Pass/ })
    await userEvent.click(pass[0]!)
    await userEvent.click(screen.getAllByRole("button", { name: /^Fail/ })[1]!)
    await userEvent.click(screen.getByRole("button", { name: /^B/ }))
    await userEvent.click(screen.getByRole("button", { name: "Submit and next" }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/evals/scores", {
      task_id: "r~q#c",
      scores: [
        { criterion: "answers", target: "A", value: { pass: true } },
        { criterion: "answers", target: "B", value: { pass: false } },
        { criterion: "preference", value: { preference: "b" } },
      ],
    }))
  })
})

describe("blind payloads", () => {
  const server = setupServer(...evalsHandlers)
  const get = async (path: string) => (await fetch(new URL(`/api/evals${path}`, location.href))).text()
  beforeAll(() => { server.listen({ onUnhandledRequest: "error" }); session.signIn("maya.lindqvist@fernhill.example") })
  afterAll(() => server.close())

  it("carry no usage, arm id, run name, origin, reason or content hash to a blind reader", async () => {
    // Blind by request (an owner), by the reviewer role (the triage suite), and as a review task.
    const compare = JSON.parse(await get("/runs/run-1/compare?blind=1")) as { rows: { columns: { item_id: string }[] }[] }
    const reviewer = JSON.parse(await get("/runs/run-4/compare")) as typeof compare
    const ids = [...compare.rows, ...reviewer.rows].flatMap((r) => r.columns.map((c) => c.item_id))
    const items = await Promise.all(ids.map((id) => get(`/items/${id}?blind=1`)))
    const payloads = [
      JSON.stringify(compare), JSON.stringify(reviewer), await get("/runs/run-4"), await get("/queues/run-1~q-1/next"), await get("/queues/run-4~q-3/next"), ...items,
    ].join("\n")
    expect(ids.length).toBeGreaterThan(4)
    for (const leak of ["usage", "enclosure", "cost_usd", "replica", "baseline", "Replica against baseline", "Triage replay", "simulated", "denied", "passthrough", "run-1:", "run-4:", "\"reason\"", "\"arm_id\":\"replica", "\"sha256\":\"b:", "Not in the recorded world"]) {
      expect(payloads.toLowerCase(), `a blind payload mentions ${leak}`).not.toContain(leak.toLowerCase())
    }
    // A run's own origin (local) is not a world answer's.
    const origins = new Set([...payloads.matchAll(/"origin":"(\w+)"/g)].map((m) => m[1]).filter((o) => o !== "local"))
    expect([...origins], "a world answer tells its origin").toEqual(["recorded"])
    // Files open by blind name through the item, and the same item has another id in each view.
    const item = JSON.parse(items[0]!) as { id: string; outputs: { id: string; blob: { sha256: string } }[] }
    expect(item.outputs[0]!.blob.sha256).toBe(`${item.id}~out1`)
    expect((await fetch(new URL(`/api/evals/blobs/${item.outputs[0]!.blob.sha256}`, location.href))).ok).toBe(true)
    const next = JSON.parse(await get("/queues/run-1~q-1/next")) as { columns: { item: { id: string } }[] }
    expect(next.columns.map((c) => c.item.id).filter((id) => ids.includes(id)), "an item id is the same in two views").toEqual([])
  })

})

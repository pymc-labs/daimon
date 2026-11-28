import { screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import { json, renderAt } from "@/test/render"
import type { RunCard, RunSummary, Workflow } from "../types"
import { RunsTab } from "./RunsTable"

const run = (number: number, over: Partial<RunSummary> = {}): RunSummary => ({
  id: `r${number}`, number, status: "running", label: null, trigger: { kind: "manual", id: "manual" }, started_at: "2026-11-05T09:00:00Z", duration_ms: null,
  current_step: null, cost_usd: 0, version: 1, test: false, step_title: null, workflow: { id: "wf1", name: "Content" }, waiting_reason: null, triggered_by: null, redriven: false, ...over,
})
const card = (number: number, stage: string, over: Partial<RunCard> = {}): RunCard => ({ run: run(number), stage, card: { title: `Post ${number}`, due: "2026-11-05" }, attention: false, date: "2026-11-05", ...over })

const wf = (spec: object) => ({ id: "wf1", spec: { schema: "daimon.workflow/v1", name: "Content", start: "a", nodes: { a: { kind: "finish" } }, ...spec } }) as unknown as Workflow
const open = (w: Workflow, search = "") => renderAt(`/w/runs${search}`, "/w/runs", <TooltipProvider><RunsTab wf={w} /></TooltipProvider>)

function server(answers: Record<string, unknown>) {
  const f = vi.fn(async (path: string) => json(answers[path] ?? []))
  vi.stubGlobal("fetch", f)
  return f
}
afterEach(() => vi.unstubAllGlobals())

const BOARD = {
  view: "board",
  columns: [
    { stage: "Draft", cards: [card(1, "Draft")] },
    { stage: "Review", cards: [card(2, "Review", { attention: true, run: run(2, { status: "waiting", waiting_reason: "approval" }) })] },
    { stage: "Done", cards: [] },
    { stage: "Failed", cards: [card(3, "Failed", { run: run(3, { status: "failed" }) })] },
  ],
}

describe("runs tab views", () => {
  it("shows no toggle when the spec has neither stages nor a card", async () => {
    server({ "/api/workflows/wf1/runs": [run(1)] })
    open(wf({}))
    expect(await screen.findByText("Run")).toBeInTheDocument()
    expect(screen.queryByRole("radio", { name: "Board" })).not.toBeInTheDocument()
  })

  it("offers the board for stages, and the calendar only when the card names a date", async () => {
    server({ "/api/workflows/wf1/runs": [run(1)] })
    open(wf({ nodes: { a: { kind: "finish", stage: "Draft" } } }))
    expect(await screen.findByRole("radio", { name: "Board" })).toBeInTheDocument()
    expect(screen.queryByRole("radio", { name: "Calendar" })).not.toBeInTheDocument()
  })

  it("lays the board out in columns with card fields and an attention badge", async () => {
    server({ "/api/workflows/wf1/runs": [run(1)], "/api/workflows/wf1/runs?view=board": BOARD })
    open(wf({ card: { fields: { title: "x" } } }))
    await userEvent.click(await screen.findByRole("radio", { name: "Board" }))
    const review = await screen.findByRole("region", { name: "Review" })
    expect(within(review).getByText("Post 2")).toBeInTheDocument()
    expect(within(review).getByText("Approval")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Draft" })).queryByText("Approval")).not.toBeInTheDocument()
    expect(screen.getAllByRole("region").map((r) => r.getAttribute("aria-label"))).toEqual(["Draft", "Review", "Done", "Failed"])
  })

  it("puts a card on its day in the calendar and asks for the month it moves to", async () => {
    const cal = (month: string, cards: RunCard[]) => ({ view: "calendar", month, cards })
    const f = server({
      "/api/workflows/wf1/runs?view=calendar&month=2026-11": cal("2026-11", [card(1, "Draft")]),
      "/api/workflows/wf1/runs?view=calendar&month=2026-12": cal("2026-12", []),
    })
    open(wf({ card: { fields: { title: "x", due: "y" }, date: "due" } }), "?view=calendar&month=2026-11")
    expect(await screen.findByText("Post 1")).toBeInTheDocument()
    expect(screen.getByText("November 2026")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Next month" }))
    expect(await screen.findByText("December 2026")).toBeInTheDocument()
    expect(f).toHaveBeenCalledWith("/api/workflows/wf1/runs?view=calendar&month=2026-12", expect.anything())
  })
})

import { useMemo } from "react"

import { cn } from "@/lib/utils"
import { useLiveRun, useRun } from "../api"
import type { RunOverlay } from "../Canvas"
import { ENDED_STEP, edgeStates, nodeRuns } from "../runs"
import { allEdges, edgeKey, titleOf } from "../spec"
import type { RunDetail, Spec, StepRun } from "../types"

export const clock = (iso: string | number) => new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
export const nodeOf = (key: string) => key.replace(/[#[].*$/, "")

/** "Draft reply", "Draft reply" visit 2, or a For each item's label. */
export const stepTitle = (spec: Spec, s: Pick<StepRun, "node" | "item">) => s.item?.label ?? titleOf(s.node, spec.nodes[s.node])

export function useOverlay(run: RunDetail | null | undefined): RunOverlay | undefined {
  return useMemo(() => {
    if (!run) return undefined
    const ended = !["queued", "running", "waiting"].includes(run.status)
    return { nodes: nodeRuns(run.steps), edges: edgeStates(run.spec, run.steps, ended), started: run.status !== "queued", ended }
  }, [run])
}

export const stepDone = (s: StepRun) => ENDED_STEP.includes(s.status)

/** Nodes not reached yet that still can be: pending rows after the steps that ran. */
export function pendingNodes(spec: Spec, run: RunDetail, overlay: RunOverlay): string[] {
  if (!["queued", "running", "waiting"].includes(run.status)) return []
  const edges = allEdges(spec)
  const out: string[] = [], seen = new Set<string>(), q = [spec.start]
  while (q.length) {
    const id = q.shift()!
    if (seen.has(id) || !spec.nodes[id]) continue
    seen.add(id)
    if (!overlay.nodes[id]) out.push(id)
    q.push(...edges.filter((e) => e.from === id && overlay.edges[edgeKey(e)] !== "not_taken").map((e) => e.to))
  }
  return out
}

/** Time until `iso`: "1d 23h", "3h 12m", "8m". */
export function until(iso: string, now = Date.now()) {
  const m = Math.max(0, Math.floor((new Date(iso).getTime() - now) / 60_000))
  if (m >= 1440) return `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`
}

/** A large clickable card for a single choice. */
export const choice = (on: boolean) => cn(
  "flex flex-col items-start gap-1 rounded-xl border bg-card px-4 py-3 text-left transition-[border-color,box-shadow] hover:border-border-strong",
  on && "border-ring ring-3 ring-ring/15 hover:border-ring",
)

/** The test run open under the design canvas (`?run=` holds its number), live, with its overlay. */
export function useTestRun(wf: string, n: string) {
  const q = useRun(wf, n)
  const live = useLiveRun(n ? q.data : undefined)
  const overlay = useOverlay(n ? live.run : undefined)
  return { ...live, overlay }
}
export type TestRun = ReturnType<typeof useTestRun>

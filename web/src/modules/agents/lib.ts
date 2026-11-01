// Pure helpers shared by the agents module.
import { channelLabel } from "@/lib/format"
import type { Effect, Place, ToolRule } from "./types"

export const EFFECT: Record<Effect, { label: string; fact: string; cls: string }> = {
  live: { label: "Live", fact: "Next message", cls: "bg-success/10 text-success" },
  session: { label: "Restarts chat", fact: "The next message starts a fresh conversation, billed once for the handoff", cls: "bg-warning/10 text-warning" },
  threads: { label: "New threads", fact: "Existing threads keep the current version", cls: "bg-foreground/[0.07] text-foreground" },
}

export const EFFORT_LABEL = { low: "Low", medium: "Medium", high: "High", xhigh: "X-high", max: "Max" } as const

export const RULE: Record<ToolRule, { label: string; cls: string }> = {
  allow: { label: "Allow", cls: "bg-muted text-muted-foreground" },
  ask: { label: "Ask", cls: "bg-warning/10 text-warning" },
  deny: { label: "Deny", cls: "bg-destructive/10 text-destructive" },
}

export const placeName = (p: Place) => (p.kind === "channel" ? channelLabel(p.name) : p.name)

type Op = { t: " " | "+" | "-"; s: string }

/** Line diff by longest common subsequence; fine for prompts and skill files. */
export function lineDiff(a: string, b: string): Op[] {
  const x = a.split("\n"), y = b.split("\n")
  const n = x.length, m = y.length
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1))
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i]![j] = x[i] === y[j] ? L[i + 1]![j + 1]! + 1 : Math.max(L[i + 1]![j]!, L[i]![j + 1]!)
  const out: Op[] = []
  let i = 0, j = 0
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push({ t: " ", s: x[i]! }); i++; j++ }
    else if (L[i + 1]![j]! >= L[i]![j + 1]!) out.push({ t: "-", s: x[i++]! })
    else out.push({ t: "+", s: y[j++]! })
  }
  while (i < n) out.push({ t: "-", s: x[i++]! })
  while (j < m) out.push({ t: "+", s: y[j++]! })
  return out
}

import { LocateFixed } from "lucide-react"
import { useEffect, useRef } from "react"

import { Money } from "@/components/common"
import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { duration } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { RunOverlay } from "../Canvas"
import { axis, timeline } from "../runs"
import { titleOf } from "../spec"
import type { RunDetail, StepRun } from "../types"
import { pendingNodes, stepTitle, until } from "./lib"
import { StepIcon } from "./parts"

const ms = (iso: string) => new Date(iso).getTime()
type Row = { key: string; step: StepRun | null; node: string; depth: number; attempt?: StepRun["attempts"][number] }

/** Steps in order of start: items and retried attempts nest under their step; pending nodes follow. */
function rowsOf(run: RunDetail, overlay: RunOverlay): Row[] {
  const out: Row[] = []
  for (const s of run.steps) {
    if (s.item) continue
    out.push({ key: s.key, step: s, node: s.node, depth: 0 })
    if (s.attempts.length > 1) for (const a of s.attempts) out.push({ key: `${s.key}@${a.n}`, step: s, node: s.node, depth: 1, attempt: a })
    for (const i of run.steps) if (i.item && i.key.startsWith(`${s.key}[`)) out.push({ key: i.key, step: i, node: i.node, depth: 1 })
  }
  for (const n of pendingNodes(run.spec, run, overlay)) out.push({ key: n, step: null, node: n, depth: 0 })
  return out
}

function took(s: StepRun, now: number) {
  if (!s.started_at) return null
  return (s.ended_at ? ms(s.ended_at) : now) - ms(s.started_at)
}

function Trail({ s, now }: { s: StepRun; now: number }) {
  const w = s.wait
  if (w?.status === "waiting") {
    return (
      <span className="flex min-w-0 items-center gap-3 text-xs text-muted-foreground">
        <span className="truncate">{w.addressees.map((p) => p.name.split(" ")[0]).join(", ") || "Event"}</span>
        {w.expires_at && <span className="shrink-0 text-warning tabular">{until(w.expires_at, now)} left</span>}
      </span>
    )
  }
  if (s.reused) return <span className="text-xs text-faint">{s.reused === "pinned" ? "Pinned" : "Reused"}</span>
  const t = took(s, now)
  return (
    <span className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground tabular">
      {t != null && <span>{duration(t)}</span>}
      {s.cost_usd > 0 && <Money usd={s.cost_usd} className="w-11 text-right" />}
    </span>
  )
}

export function StepsPanel({ run, overlay, now, selected, select, follow, setFollow, mode, setMode, className }: {
  run: RunDetail; overlay: RunOverlay; now: number; selected: string | null; select: (key: string) => void
  follow: boolean; setFollow: (on: boolean) => void; mode: "list" | "timeline"; setMode: (m: "list" | "timeline") => void; className?: string
}) {
  const rows = rowsOf(run, overlay)
  const list = useRef<HTMLDivElement>(null)
  const active = ["queued", "running", "waiting"].includes(run.status)
  useEffect(() => {
    if (!follow || !run.current_step) return
    list.current?.querySelector(`[data-key="${CSS.escape(run.current_step)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" })
  }, [follow, run.current_step, run.steps.length])
  return (
    <section className={cn("flex min-h-0 flex-col", className)}>
      <header className="flex h-11 shrink-0 items-center gap-2 border-b pr-2 pl-4">
        <h2 className="mr-auto text-sm font-medium">Steps</h2>
        {active && (
          <Button variant="ghost" size="sm" aria-pressed={follow} onClick={() => setFollow(!follow)} className={cn("text-muted-foreground", follow && "bg-accent text-foreground")}>
            <LocateFixed />Follow
          </Button>
        )}
        <ToggleGroup type="single" variant="segmented" size="sm" value={mode} onValueChange={(v) => v && setMode(v as "list" | "timeline")}>
          <ToggleGroupItem value="list" className="px-2.5 text-xs">List</ToggleGroupItem>
          <ToggleGroupItem value="timeline" className="px-2.5 text-xs">Timeline</ToggleGroupItem>
        </ToggleGroup>
      </header>
      <div ref={list} className="min-h-0 flex-1 overflow-y-auto py-1">
        {mode === "list" ? rows.map((r) => <ListRow key={r.key} r={r} run={run} now={now} selected={selected} select={select} />) : <Timeline run={run} rows={rows} now={now} selected={selected} select={select} />}
      </div>
    </section>
  )
}

function ListRow({ r, run, now, selected, select }: { r: Row; run: RunDetail; now: number; selected: string | null; select: (k: string) => void }) {
  const s = r.step
  const current = s && run.current_step === s.key
  if (r.attempt) {
    const a = r.attempt
    return (
      <div className="flex h-7 items-center gap-2 pr-4 pl-11 text-xs text-muted-foreground">
        <span className="tabular">Attempt {a.n}</span>
        {a.error && <span className={cn("truncate", a.status === "failed" ? "text-destructive" : "text-muted-foreground")}>{a.error.class}</span>}
        <span className="ml-auto shrink-0 tabular">{a.duration_ms != null ? duration(a.duration_ms) : a.status === "running" ? "Running" : ""}</span>
      </div>
    )
  }
  return (
    <button
      data-key={r.key} disabled={!s} onClick={() => s && select(s.key)}
      className={cn(
        "flex h-8 w-full items-center gap-2 pr-4 text-left text-sm transition-colors hover:bg-accent disabled:hover:bg-transparent",
        r.depth ? "pl-11 text-[13px]" : "pl-4",
        s && selected === s.key && "bg-accent", current && "font-medium",
        s && s.started_at && ms(s.started_at) > now - 1500 && "animate-in fade-in slide-in-from-top-1 duration-500",
      )}
    >
      <StepIcon status={s?.status ?? "pending"} />
      <span className={cn("min-w-0 flex-1 truncate", !s && "text-muted-foreground")}>
        {s ? stepTitle(run.spec, s) : titleOf(r.node, run.spec.nodes[r.node])}
        {s && s.visit > 1 && <span className="ml-1.5 text-xs font-normal text-muted-foreground tabular">Visit {s.visit}</span>}
        {s?.progress && <span className="ml-1.5 text-xs font-normal text-muted-foreground tabular">{s.progress.done} of {s.progress.total}</span>}
      </span>
      {s ? <Trail s={s} now={now} /> : <span className="text-xs text-faint">Pending</span>}
    </button>
  )
}

const BAR: Record<string, string> = {
  succeeded: "bg-success/60", failed: "bg-destructive/70", unknown: "bg-destructive/70", running: "bg-link/70 animate-pulse", cancelled: "bg-faint", skipped: "bg-faint",
}

function Timeline({ run, rows, now, selected, select }: { run: RunDetail; rows: Row[]; now: number; selected: string | null; select: (k: string) => void }) {
  const steps = rows.filter((r) => r.step && !r.attempt).map((r) => r.step!)
  const from = ms(run.started_at)
  const to = run.ended_at ? ms(run.ended_at) : now
  const bars = new Map(timeline(steps, from, to).map((t) => [t.key, t.bars]))
  const ticks = axis(from, to, 5)
  const pct = (t: number) => `${((t - from) / Math.max(to - from, 1)) * 100}%`
  return (
    <div className="grid grid-cols-[136px_1fr] pr-4">
      <span />
      <div className="relative mb-1 h-6 border-b text-[11px] text-faint tabular">
        {ticks.map((t) => <span key={t} style={{ left: pct(t) }} className={cn("absolute top-1 -translate-x-1/2 whitespace-nowrap", (t - from) / Math.max(to - from, 1) < 0.1 && "translate-x-0")}>{duration(t - from).replace(/\.0 s$/, "s")}</span>)}
      </div>
      {steps.map((s) => (
        <button key={s.key} data-key={s.key} onClick={() => select(s.key)}
          className={cn("col-span-2 grid h-8 grid-cols-subgrid items-center text-left hover:bg-accent", selected === s.key && "bg-accent")}>
          <span className={cn("flex min-w-0 items-center gap-2 text-[13px]", s.item ? "pl-9" : "pl-4")}>
            <StepIcon status={s.status} /><span className="truncate">{stepTitle(run.spec, s)}</span>
          </span>
          <span className="relative h-full">
            {ticks.map((t) => <span key={t} style={{ left: pct(t) }} className="absolute inset-y-0 w-px bg-border/60" />)}
            {(bars.get(s.key) ?? []).map((b, i) => (
              <span key={i} style={{ left: `${b.left * 100}%`, width: `${b.width * 100}%` }}
                className={cn("absolute top-1/2 h-3 -translate-y-1/2 rounded-sm", b.kind === "wait" ? "border border-dashed border-warning bg-warning/20" : BAR[s.status] ?? "bg-link/50")} />
            ))}
          </span>
        </button>
      ))}
    </div>
  )
}

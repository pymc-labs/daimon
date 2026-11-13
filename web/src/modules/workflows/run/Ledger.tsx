import { ChevronDown, LoaderCircle, ShieldCheck, ShieldX } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Mono } from "@/components/common"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useVerify } from "../api"
import { cn } from "@/lib/utils"
import { groupByStep } from "../runs"
import type { RunDetail, RunEvent } from "../types"
import { clock, nodeOf, stepTitle } from "./lib"
import { StepIcon } from "./parts"

const FAMILIES = ["run", "step", "wait", "proposal", "budget", "delivery"] as const
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

export function Chain({ run }: { run: RunDetail }) {
  const v = useVerify(run)
  if (v.isPending) return <span className="flex items-center gap-1.5 text-xs text-muted-foreground"><LoaderCircle className="size-3.5 animate-spin" />Verifying</span>
  if (!v.data?.ok) return <span className="flex items-center gap-1.5 text-xs text-destructive"><ShieldX className="size-3.5" /><span className="text-foreground">Chain broken</span></span>
  return <span className="flex items-center gap-1.5 text-xs text-success" title={`Head ${run.head_hash.slice(0, 12)}`}><ShieldCheck className="size-3.5" /><span className="text-foreground">Verified</span></span>
}

/** The run's ledger: Compact groups rows per step, Raw lists every row; new rows animate in. */
export function Ledger({ run, events, fresh, select, open, setOpen, className, raw }: {
  run: RunDetail; events: RunEvent[]; fresh: number; select: (key: string) => void; open: boolean; setOpen: (o: boolean) => void; className?: string; raw?: boolean
}) {
  const [mode, setMode] = useState<"compact" | "raw">(raw ? "raw" : "compact")
  const [family, setFamily] = useState("all")
  const shown = family === "all" ? events : events.filter((e) => e.type.startsWith(`${family}.`))
  const body = useRef<HTMLDivElement>(null)
  // Stay pinned to the newest row unless the reader scrolled up.
  const pinned = useRef(true)
  useEffect(() => { const b = body.current; if (b && pinned.current) b.scrollTop = b.scrollHeight }, [shown.length, mode, open])
  return (
    <section className={cn("flex shrink-0 flex-col border-t bg-background", open ? "h-64" : "h-11", className)}>
      <header className="flex h-11 shrink-0 items-center gap-3 pr-2 pl-2 max-sm:gap-2">
        <button className="flex h-8 items-center gap-1.5 rounded-md px-2 text-sm font-medium hover:bg-accent" onClick={() => setOpen(!open)} aria-expanded={open}>
          <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", !open && "-rotate-90")} />Ledger
        </button>
        <span className="text-xs text-muted-foreground tabular">{events.length} events</span>
        <Chain run={run} />
        {open && (
          <span className="ml-auto flex items-center gap-2">
            <Select value={family} onValueChange={setFamily}>
              <SelectTrigger size="sm" className="h-8 w-32 text-xs max-sm:w-24"><SelectValue /></SelectTrigger>
              <SelectContent align="end">
                <SelectItem value="all">All types</SelectItem>
                {FAMILIES.map((f) => <SelectItem key={f} value={f}>{cap(f)}</SelectItem>)}
              </SelectContent>
            </Select>
            <ToggleGroup type="single" variant="segmented" size="sm" value={mode} onValueChange={(v) => v && setMode(v as "compact" | "raw")}>
              <ToggleGroupItem value="compact" className="px-2.5 text-xs">Compact</ToggleGroupItem>
              <ToggleGroupItem value="raw" className="px-2.5 text-xs">Raw</ToggleGroupItem>
            </ToggleGroup>
          </span>
        )}
      </header>
      {open && (
        <div ref={body} onScroll={(e) => { const b = e.currentTarget; pinned.current = b.scrollHeight - b.scrollTop - b.clientHeight < 24 }}
          className="min-h-0 flex-1 overflow-y-auto border-t text-xs">
          {mode === "raw" ? <Raw run={run} events={shown} fresh={fresh} /> : <Compact run={run} events={shown} fresh={fresh} select={select} />}
        </div>
      )}
    </section>
  )
}

const NEW = "animate-in fade-in slide-in-from-bottom-1 bg-link/5 duration-700"

function Raw({ run, events, fresh }: { run: RunDetail; events: RunEvent[]; fresh: number }) {
  const steps = new Map(run.steps.map((s) => [s.key, s]))
  return (
    <table className="w-full">
      <thead className="sticky top-0 z-[1] bg-background text-left text-muted-foreground">
        <tr className="h-8 border-b [&>th]:font-normal"><th className="w-14 pl-4">Seq</th><th className="w-20">Time</th><th className="w-48">Type</th><th className="w-44 max-lg:hidden">Step</th><th className="w-36 max-md:hidden">Actor</th><th className="pr-4">Detail</th></tr>
      </thead>
      <tbody>
        {events.map((e) => (
          <tr key={e.seq} className={cn("h-7 border-b border-border/60 last:border-0", e.seq > fresh && NEW)}>
            <td className="pl-4 text-faint tabular">{e.seq}</td>
            <td className="text-muted-foreground tabular">{clock(e.at)}</td>
            <td><Mono>{e.type}</Mono></td>
            <td className="max-w-44 truncate max-lg:hidden">{e.step ? stepTitle(run.spec, steps.get(e.step) ?? { node: nodeOf(e.step), item: null }) : <span className="text-faint">Run</span>}</td>
            <td className="truncate text-muted-foreground max-md:hidden">{e.actor}</td>
            <td className="max-w-0 truncate pr-4">{e.detail}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Compact({ run, events, fresh, select }: { run: RunDetail; events: RunEvent[]; fresh: number; select: (key: string) => void }) {
  const steps = new Map(run.steps.map((s) => [s.key, s]))
  return (
    <div className="py-1">
      {groupByStep(events).map((g) => {
        const s = g.step ? steps.get(g.step) : undefined
        const last = g.events.at(-1)!
        const title = s ? stepTitle(run.spec, s) : g.step ? stepTitle(run.spec, { node: nodeOf(g.step), item: null }) : "Run"
        return (
          <button key={g.events[0]!.seq} onClick={() => s && select(s.key)} disabled={!s}
            className={cn("grid h-8 w-full grid-cols-[72px_minmax(0,200px)_1fr_auto] items-center gap-3 px-4 text-left hover:bg-accent disabled:hover:bg-transparent max-sm:grid-cols-[minmax(0,140px)_1fr_auto]", last.seq > fresh && NEW)}>
            <span className="text-muted-foreground tabular max-sm:hidden">{clock(g.events[0]!.at)}</span>
            <span className="flex min-w-0 items-center gap-2 text-sm">{s ? <StepIcon status={s.status} /> : <span className="size-3.5 shrink-0" />}<span className="truncate">{title}</span></span>
            <span className="truncate text-muted-foreground">{last.detail}</span>
            <span className="text-faint tabular">{g.events.length}</span>
          </button>
        )
      })}
    </div>
  )
}

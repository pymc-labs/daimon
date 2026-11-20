import { useQuery } from "@tanstack/react-query"
import { Check, ExternalLink, FastForward, Inbox, ShieldAlert, X } from "lucide-react"
import { useState } from "react"
import { Link, useSearchParams } from "react-router"

import { Money } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Sheet, SheetContent, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import { useParam } from "@/lib/filters"
import { duration } from "@/lib/format"
import { cn } from "@/lib/utils"
import { isActive, useNow, useRunById, useRuns, useStartRun } from "../api"
import { KIND } from "../kinds"
import { titleOf, writes } from "../spec"
import type { Delivery, Draft, RunDetail, RunEvent, Workflow } from "../types"
import { RunBadge } from "../ui"
import { Ledger } from "./Ledger"
import { choice, type TestRun } from "./lib"
import { Fact } from "./parts"
import { ForkDialog } from "./RunDialogs"
import { StepView } from "./StepDrawer"
import { StepsPanel } from "./Steps"

/** Start a test run of the draft: inputs (or the last webhook delivery), live writes, and outputs pinned from the last run. */
export function TestRunSheet({ wf, draft, open, onOpenChange }: { wf: Workflow; draft: Draft; open: boolean; onOpenChange: (o: boolean) => void }) {
  const spec = draft.spec
  const start = useStartRun(wf.id)
  const [, setParams] = useSearchParams()
  const props = Object.entries(spec.inputs?.properties ?? {})
  const [vals, setVals] = useState<Record<string, string>>({})
  const [delivery, setDelivery] = useState<Delivery | null>(null)
  const [live, setLive] = useState(false)
  const [pinned, setPinned] = useState<string[]>([])
  const webhook = (spec.triggers ?? []).some((t) => t.kind === "webhook")
  const last = useQuery({
    queryKey: ["workflows", wf.id, "deliveries", "last"], enabled: open && webhook, retry: false,
    queryFn: () => api.get<Delivery>(`/api/workflows/${wf.id}/deliveries/last`),
  })
  const prev = useRuns(wf.id).data?.find((r) => r.status === "succeeded" || r.status === "failed")
  const prevRun = useRunById(open ? prev?.id ?? null : null).data
  const done = (prevRun?.steps ?? []).filter((s) => !s.item && s.status === "succeeded" && spec.nodes[s.node])
  const writers = Object.entries(spec.nodes).filter(([, n]) => writes(n)).map(([id, n]) => titleOf(id, n))
  const missing = (spec.inputs?.required ?? []).some((k) => !vals[k]?.trim())
  const use = (d: Delivery) => {
    setDelivery(d)
    setVals(Object.fromEntries(Object.entries(d.inputs).map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)])))
  }
  const go = () => start.mutate({ inputs: vals, test: { live_writes: live, pinned, pinned_from: pinned.length ? prevRun?.id ?? null : null } }, {
    onSuccess: (r) => { onOpenChange(false); setParams((p) => { p.set("run", String(r.number)); p.delete("node"); return p }, { replace: true }) },
  })
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="gap-0 p-0 sm:max-w-[480px]">
        <SheetHeader className="h-14 flex-row items-center border-b py-0 pr-12 pl-5"><SheetTitle>Test run</SheetTitle><span className="ml-auto text-xs text-muted-foreground tabular">Draft revision {draft.revision}</span></SheetHeader>
        <form id="test-run" className="min-h-0 flex-1 space-y-6 overflow-y-auto px-5 py-5" onSubmit={(e) => { e.preventDefault(); if (!missing) go() }}>
          <section className="space-y-3">
            <div className="flex h-8 items-center justify-between">
              <h3 className="text-sm font-medium">Inputs</h3>
              {webhook && (
                <Button type="button" variant="outline" size="sm" disabled={!last.data} onClick={() => last.data && use(last.data)}><Inbox />Use last webhook delivery</Button>
              )}
            </div>
            {delivery && (
              <div className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-xs">
                <ShieldAlert className="size-3.5 text-warning" /><span className="font-medium">Untrusted</span>
                <span className="text-muted-foreground">Delivery</span><span className="font-mono">{delivery.id}</span>
                <button type="button" className="ml-auto text-muted-foreground hover:text-foreground" aria-label="Clear delivery" onClick={() => { setDelivery(null); setVals({}) }}><X className="size-3.5" /></button>
              </div>
            )}
            {props.map(([k, f]) => (
              <div key={k} className="space-y-1.5">
                <Label htmlFor={`t-${k}`}>{f.title ?? k}{spec.inputs?.required?.includes(k) && <span className="text-faint">Required</span>}</Label>
                <Input id={`t-${k}`} value={vals[k] ?? ""} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} className="h-8" />
              </div>
            ))}
            {!props.length && <p className="text-sm text-faint">No inputs</p>}
          </section>
          <section className="space-y-3">
            <h3 className="text-sm font-medium">Writes</h3>
            <label className={cn(choice(live), "cursor-pointer flex-row items-center gap-3")}>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-medium">Live writes</span>
                <span className="block truncate text-xs text-muted-foreground">{writers.length ? writers.join(", ") : "No write steps"}</span>
              </span>
              <Switch checked={live} onCheckedChange={setLive} disabled={!writers.length} />
            </label>
          </section>
          {done.length > 0 && prevRun && (
            <section className="space-y-3">
              <div className="flex h-5 items-center justify-between">
                <h3 className="text-sm font-medium">Pinned outputs</h3>
                <span className="text-xs text-muted-foreground tabular">From run {prevRun.number}</span>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                {done.map((s) => {
                  const n = spec.nodes[s.node]!
                  const K = KIND[n.kind]
                  const on = pinned.includes(s.node)
                  return (
                    <button key={s.key} type="button" aria-pressed={on} onClick={() => setPinned(on ? pinned.filter((x) => x !== s.node) : [...pinned, s.node])}
                      className={cn(choice(on), "flex-row items-center gap-2.5")}>
                      <K.icon className={cn("size-4 shrink-0", K.text)} />
                      <span className="min-w-0 flex-1 truncate text-sm">{titleOf(s.node, n)}</span>
                      <span className={cn("flex size-4 shrink-0 items-center justify-center rounded-full border", on && "border-ring bg-ring text-background")}>{on && <Check className="size-3" />}</span>
                    </button>
                  )
                })}
              </div>
            </section>
          )}
        </form>
        <SheetFooter className="mt-0 border-t px-5 py-3">
          <div className="flex justify-end gap-2 max-sm:[&>*]:flex-1">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" form="test-run" disabled={missing || start.isPending}>Start test run</Button>
          </div>
          {start.error && <p className="text-right text-sm text-destructive">{start.error.message}</p>}
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}

/** The test run under the canvas: steps and the raw ledger. */
export function SplitRun({ wf, live }: { wf: Workflow; live: TestRun }) {
  const { run, overlay } = live
  const [, setRun] = useParam("run")
  const [step, setStep] = useParam("step")
  const [fork, setFork] = useState(false)
  const [follow, setFollow] = useState(true)
  const [mode, setMode] = useState<"list" | "timeline">("list")
  const now = useNow(!!run && isActive(run))
  if (!run || !overlay) return <section className="h-[320px] shrink-0 border-t bg-background" />
  const took = run.duration_ms ?? (run.ended_at ? Date.parse(run.ended_at) : now) - Date.parse(run.started_at)
  return (
    <section aria-label="Test run" className="flex h-[320px] shrink-0 flex-col border-t bg-background animate-in fade-in slide-in-from-bottom-4">
      <header className="flex h-11 shrink-0 items-center gap-4 border-b pr-2 pl-4 text-sm">
        <RunBadge status={run.status} />
        <span className="font-medium tabular">Test run {run.number}</span>
        <Fact label="Duration" className="max-sm:hidden"><span className="tabular">{duration(took)}</span></Fact>
        <Fact label="Cost" className="max-sm:hidden"><Money usd={run.cost_usd} /></Fact>
        <span className="ml-auto flex items-center gap-1">
          <Button variant="outline" size="sm" onClick={() => setFork(true)}><FastForward />Run from here</Button>
          <Button variant="ghost" size="icon-sm" asChild aria-label="Open run"><Link to={`/workflows/${wf.id}/runs/${run.number}`}><ExternalLink /></Link></Button>
          <Button variant="ghost" size="icon-sm" aria-label="Close" onClick={() => setRun(null)}><X /></Button>
        </span>
      </header>
      <div className="flex min-h-0 flex-1">
        <StepsPanel run={run} overlay={overlay} now={now} selected={step || null} select={(k) => setStep(k)} follow={follow} setFollow={setFollow} mode={mode} setMode={setMode}
          className="w-[440px] shrink-0 border-r max-md:w-full max-md:border-r-0" />
        <RawLedger run={run} events={live.events} fresh={live.fresh} select={setStep} />
      </div>
      <Drawer param="step" title={`Test run ${run.number}`} wide ids={run.steps.map((s) => s.key)}>
        {(k) => <StepView run={run} events={live.events} stepKey={k} now={now} />}
      </Drawer>
      {fork && <ForkDialog run={run} step={step || undefined} version="draft" open onOpenChange={setFork} />}
    </section>
  )
}

function RawLedger({ run, events, fresh, select }: { run: RunDetail; events: RunEvent[]; fresh: number; select: (k: string) => void }) {
  const [open, setOpen] = useState(true)
  return <Ledger run={run} events={events} fresh={fresh} select={select} open={open} setOpen={setOpen} raw className="h-auto min-w-0 flex-1 border-t-0 max-md:hidden" />
}

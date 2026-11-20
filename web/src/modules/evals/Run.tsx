import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Columns2, Gavel, Users, XCircle } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate, useParams } from "react-router"

import { StatStrip } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { E, isLive, useCases, useRun, useRunItems, useSummary } from "./api"
import { ApproveBar, EstimateView } from "./Approve"
import { RunPill } from "./bits"
import { Summary } from "./Summary"
import type { Estimate, ItemStatus, Queue, Run as RunDoc, RunItemSummary } from "./types"

const DOT: Record<ItemStatus, string> = { pending: "bg-border-strong", running: "animate-pulse bg-link", done: "bg-success", error: "bg-destructive", skipped: "bg-faint", cancelled: "bg-faint" }

/** Case by arm: one square per repeat, coloured by where the item stands. */
function Grid({ run, items }: { run: RunDoc; items: RunItemSummary[] }) {
  const cases = useCases(run.suite.id, run.suite.version).data ?? []
  const ids = run.case_ids ?? cases.map((c) => c.id)
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full border-collapse text-sm">
        <thead><tr className="border-b text-left text-xs text-muted-foreground"><th className="px-3 py-2 font-medium">Case</th>{run.arms.map((a) => <th key={a.id} className="px-3 py-2 font-medium">{a.label}</th>)}</tr></thead>
        <tbody>
          {ids.map((id) => (
            <tr key={id} className="border-b last:border-b-0 hover:bg-accent/40">
              <td className="max-w-64 truncate px-3 py-1.5"><Link className="hover:underline" to={`/evals/runs/${run.id}/compare?case=${id}`}>{cases.find((c) => c.id === id)?.title ?? id}</Link></td>
              {run.arms.map((a) => (
                <td key={a.id} className="px-3 py-1.5">
                  <span className="flex gap-1">{items.filter((i) => i.case_id === id && i.arm_id === a.id).sort((x, y) => x.repeat - y.repeat).map((i) => <span key={i.id} title={i.status} aria-label={i.status} className={cn("size-3 rounded-sm", DOT[i.status])} />)}</span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Judge({ run, open, onOpenChange }: { run: RunDoc; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const ids = run.judges?.map((j) => j.id) ?? []
  const [estimate, setEstimate] = useState<Estimate | null>(null)
  const ask = useMutation({ mutationFn: () => api.post<Estimate>(`${E}/runs/${run.id}/judge`, { judge_ids: ids }), onSuccess: setEstimate })
  const go = useMutation({
    mutationFn: (cap: number) => api.post(`${E}/runs/${run.id}/judge`, { judge_ids: ids, cap_usd: cap }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["evals"] }); onOpenChange(false) },
  })
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) setEstimate(null) }}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader><DialogTitle>Run judges</DialogTitle></DialogHeader>
        {estimate ? <EstimateView estimate={estimate} arms={run.arms} /> : <p className="text-sm text-muted-foreground">{ids.join(", ")}</p>}
        {(ask.error ?? go.error) && <p role="alert" className="text-sm text-destructive">{(ask.error ?? go.error)!.message}</p>}
        <DialogFooter className="sm:justify-end">
          {estimate ? <ApproveBar estimate={estimate} label="Approve and judge" pending={go.isPending} onApprove={(c) => go.mutate(c)} /> : <Button disabled={ask.isPending || !ids.length} onClick={() => ask.mutate()}>Estimate</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function NewQueue({ run, open, onOpenChange }: { run: RunDoc; open: boolean; onOpenChange: (o: boolean) => void }) {
  const go = useNavigate()
  const [name, setName] = useState(`Review of ${run.name ?? run.id}`)
  const [blind, setBlind] = useState(true)
  const [layout, setLayout] = useState<Queue["layout"]>(run.arms.length > 1 ? "side_by_side" : "single")
  const make = useMutation({ mutationFn: () => api.post<Queue>(`${E}/queues`, { run_id: run.id, name, blind, layout }), onSuccess: (q) => go(`/evals/queues/${q.id}`) })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>Review queue</DialogTitle></DialogHeader>
        <div className="space-y-3 text-sm">
          <label className="grid gap-1.5"><span className="text-muted-foreground">Name</span><Input value={name} onChange={(e) => setName(e.target.value)} className="h-8" /></label>
          <div className="flex items-center justify-between"><span>Blind</span><Switch checked={blind} onCheckedChange={setBlind} aria-label="Blind" /></div>
          <div className="flex items-center justify-between"><span>Side by side</span><Switch checked={layout === "side_by_side"} onCheckedChange={(v) => setLayout(v ? "side_by_side" : "single")} aria-label="Side by side" /></div>
        </div>
        {make.error && <p role="alert" className="text-sm text-destructive">{make.error.message}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!name.trim() || make.isPending} onClick={() => make.mutate()}>Create</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function Run() {
  const { run: id = "" } = useParams()
  const qc = useQueryClient()
  const confirm = useConfirm()
  const run = useRun(id)
  const r = run.data
  const live = isLive(r)
  const items = useRunItems(id, live)
  const finished = !!r && ["done", "partial", "cancelled", "failed"].includes(r.status)
  const summary = useSummary(id, !!r && (finished || r.status === "running"), live)
  const [judging, setJudging] = useState(false)
  const [queueing, setQueueing] = useState(false)
  const refresh = () => qc.invalidateQueries({ queryKey: ["evals"] })
  const cancel = useMutation({ mutationFn: () => api.post(`${E}/runs/${id}/cancel`), onSuccess: refresh })
  const approve = useMutation({ mutationFn: (cap: number) => api.post(`${E}/approvals/${id}`, { cap_usd: cap }), onSuccess: refresh })
  const list = items.data ?? []
  const done = list.filter((i) => !["pending", "running"].includes(i.status)).length
  const spend = r?.spend?.total_usd ?? 0
  const cap = r?.approval?.cap_usd
  return (
    <Page
      crumbs={[{ label: "Evals", to: "/evals" }, { label: r?.suite.id ?? "Suite", to: r ? `/evals/suites/${r.suite.id}` : undefined }, { label: r?.name ?? id }]}
      actions={r && (
        <>
          {live && <Button variant="outline" size="sm" disabled={cancel.isPending} onClick={async () => { if (await confirm({ title: "Cancel run?", body: "Items still running stop. Finished items keep their results.", action: "Cancel run", destructive: true })) cancel.mutate() }}><XCircle />Cancel</Button>}
          {finished && !!r.judges?.length && <Button variant="outline" size="sm" onClick={() => setJudging(true)}><Gavel />Judge</Button>}
          {finished && <Button variant="outline" size="sm" onClick={() => setQueueing(true)}><Users />Review</Button>}
          {r.status !== "estimated" && r.status !== "draft" && <Button size="sm" asChild><Link to={`/evals/runs/${id}/compare`}><Columns2 />Compare</Link></Button>}
        </>
      )}
    >
      <QueryState query={run} skeleton={<Skeleton className="h-24" />}>
        {(r) => (
          <div className="mx-auto max-w-[1040px] space-y-8">
            <StatStrip stats={[
              { label: "Status", value: <RunPill status={r.status} /> },
              { label: "Items", value: list.length ? `${done} of ${list.length}` : "–" },
              { label: "Spend", value: cap ? `${usd(spend)} of ${usd(cap)}` : usd(spend) },
              { label: "Repeats", value: r.repeats },
            ]} />
            {cap && <div className="-mt-5 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-label="Spend against cap" aria-valuenow={spend} aria-valuemax={cap}><div className={cn("h-full", spend / cap > 0.9 ? "bg-warning" : "bg-teal")} style={{ width: `${Math.min(100, (spend / cap) * 100)}%` }} /></div>}
            {r.status === "estimated" && r.estimate && (
              <section className="space-y-3">
                <h2 className="text-sm font-medium">Estimate</h2>
                <EstimateView estimate={r.estimate} arms={r.arms} />
                {approve.error && <p role="alert" className="text-sm text-destructive">{approve.error.message}</p>}
                <ApproveBar estimate={r.estimate} pending={approve.isPending} onApprove={(c) => approve.mutate(c)} />
              </section>
            )}
            {list.length > 0 && <section><h2 className="mb-2 text-sm font-medium">Progress</h2><Grid run={r} items={list} /></section>}
            {summary.data && <section><h2 className="mb-3 text-sm font-medium">Summary</h2><Summary s={summary.data} arms={r.arms} /></section>}
          </div>
        )}
      </QueryState>
      {r && <Judge run={r} open={judging} onOpenChange={setJudging} />}
      {r && <NewQueue key={queueing ? "open" : "closed"} run={r} open={queueing} onOpenChange={setQueueing} />}
    </Page>
  )
}

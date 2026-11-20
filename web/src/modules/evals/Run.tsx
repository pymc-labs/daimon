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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { E, isLive, useCases, useIsAdmin, useRole, useRun, useRunItems, useSummary } from "./api"
import { ApproveBar, EstimateView } from "./Approve"
import { canEdit, isBlindRun, RunPill } from "./bits"
import { Summary } from "./Summary"
import type { ItemStatus, Judge as JudgeDoc, JudgeOutcome, JudgeRequest, QueueDraft, QueueView, Run as RunDoc, RunItemSummary } from "./types"

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

/** Estimate one judge over a finished run, then run it under a spend ceiling. Ceilings above the admin limit are refused by the server, which says who to ask. */
function Judge({ run, open, onOpenChange }: { run: RunDoc; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const admin = useIsAdmin()
  const judges = run.judges ?? []
  const [id, setId] = useState(judges[0]?.id ?? "")
  const judge = judges.find((j) => j.id === id)
  const request = (j: JudgeDoc, cap?: number): JudgeRequest => ({ id: j.id, kind: j.kind, models: j.models, aggregate: j.aggregate ?? "majority", batch: false, approve_cap_usd: cap })
  const [outcome, setOutcome] = useState<JudgeOutcome | null>(null)
  const ask = useMutation({ mutationFn: () => api.post<JudgeOutcome>(`${E}/runs/${run.id}/judge`, request(judge!)), onSuccess: setOutcome })
  const go = useMutation({
    mutationFn: (cap: number) => api.post<JudgeOutcome>(`${E}/runs/${run.id}/judge`, request(judge!, cap)),
    onSuccess: (o) => { setOutcome(o); void qc.invalidateQueries({ queryKey: ["evals"] }) },
  })
  const done = outcome?.ran
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) { setOutcome(null); ask.reset(); go.reset() } }}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader><DialogTitle>Run judges</DialogTitle></DialogHeader>
        {outcome ? <EstimateView estimate={outcome.estimate} arms={run.arms} /> : (
          <Select value={id} onValueChange={setId}>
            <SelectTrigger aria-label="Judge" className="h-8"><SelectValue /></SelectTrigger>
            <SelectContent>{judges.map((j) => <SelectItem key={j.id} value={j.id}>{j.id}</SelectItem>)}</SelectContent>
          </Select>
        )}
        {done && <p role="status" className="text-sm">{outcome.scores} scores for {usd(outcome.spent_usd)}{outcome.skipped > 0 && `, ${outcome.skipped} skipped for lack of room`}{outcome.failed > 0 && `, ${outcome.failed} failed`}</p>}
        {(ask.error ?? go.error) && <p role="alert" className="text-sm text-destructive">{(ask.error ?? go.error)!.message}</p>}
        <DialogFooter className="sm:justify-end">
          {done ? <Button onClick={() => onOpenChange(false)}>Done</Button>
            : outcome ? <ApproveBar estimate={outcome.estimate} label="Approve and judge" limit={outcome.estimate.admin_approval_usd} admin={admin} pending={go.isPending} onApprove={(c) => go.mutate(c)} />
            : <Button disabled={ask.isPending || !judge} onClick={() => ask.mutate()}>Estimate</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function NewQueue({ run, open, onOpenChange }: { run: RunDoc; open: boolean; onOpenChange: (o: boolean) => void }) {
  const go = useNavigate()
  // A blind queue's name reaches reviewers, so the default says nothing of the run.
  const [typed, setName] = useState<string | null>(null)
  const [blind, setBlind] = useState(true)
  const name = typed ?? (blind ? "Review queue" : `Review of ${run.name ?? run.id}`)
  const [layout, setLayout] = useState<QueueDraft["layout"]>(run.arms.length > 1 ? "side_by_side" : "single")
  const [id] = useState(() => `queue-${crypto.randomUUID().slice(0, 8)}`)
  const draft: QueueDraft = { id, name, blind, layout }
  const make = useMutation({ mutationFn: () => api.post<QueueView>(`${E}/queues`, { run_id: run.id, queue: draft }), onSuccess: (q) => go(`/evals/queues/${q.ref}`) })
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
  const doc = run.data
  const r = doc && !isBlindRun(doc) ? doc : undefined
  const role = useRole(doc?.suite.id ?? "")
  const admin = useIsAdmin()
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
  const cap = r?.approval?.cap_usd, asked = r?.ext?.["daimon.requested_cap_usd"]
  const editor = canEdit(role)
  if (doc && !r) return <Page crumbs={[{ label: "Evals", to: "/evals" }, { label: "Review" }]}><div className="py-16 text-center text-sm text-muted-foreground">Reviewers open their work from Evals.<br /><Link className="text-link hover:underline" to="/evals">Back to evals</Link></div></Page>
  return (
    <Page
      crumbs={[{ label: "Evals", to: "/evals" }, { label: r?.suite.id ?? "Suite", to: r ? `/evals/suites/${r.suite.id}` : undefined }, { label: r?.name ?? id }]}
      actions={r && (
        <>
          {live && editor && <Button variant="outline" size="sm" disabled={cancel.isPending} onClick={async () => { if (await confirm({ title: "Cancel run?", body: "Items still running stop. Finished items keep their results.", action: "Cancel run", destructive: true })) cancel.mutate() }}><XCircle />Cancel</Button>}
          {finished && editor && !!r.judges?.length && <Button variant="outline" size="sm" onClick={() => setJudging(true)}><Gavel />Judge</Button>}
          {finished && editor && <Button variant="outline" size="sm" onClick={() => setQueueing(true)}><Users />Review</Button>}
          {r.status !== "estimated" && r.status !== "draft" && <Button size="sm" asChild><Link to={`/evals/runs/${id}/compare`}><Columns2 />Compare</Link></Button>}
        </>
      )}
    >
      <QueryState query={run} skeleton={<Skeleton className="h-24" />}>
        {() => r && (
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
                {admin ? (
                  <>
                    {approve.error && <p role="alert" className="text-sm text-destructive">{approve.error.message}</p>}
                    <ApproveBar estimate={r.estimate} initial={asked} pending={approve.isPending} onApprove={(c) => approve.mutate(c)} />
                  </>
                ) : <p role="status" className="text-right text-sm text-muted-foreground">Awaiting admin approval{asked ? ` of up to ${usd(asked)}` : ""}. Ask an admin.</p>}
              </section>
            )}
            {list.length > 0 && <section><h2 className="mb-2 text-sm font-medium">Progress</h2><Grid run={r} items={list} /></section>}
            {summary.data && <section><h2 className="mb-3 text-sm font-medium">Summary</h2><Summary s={summary.data} arms={r.arms} /></section>}
          </div>
        )}
      </QueryState>
      {r && editor && <Judge run={r} open={judging} onOpenChange={setJudging} />}
      {r && editor && <NewQueue key={queueing ? "open" : "closed"} run={r} open={queueing} onOpenChange={setQueueing} />}
    </Page>
  )
}

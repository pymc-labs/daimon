import { ReactFlowProvider } from "@xyflow/react"
import { CircleHelp, CircleX, Download, FastForward, Hourglass, Play, RotateCcw, ShieldCheck, Square, X } from "lucide-react"
import { useEffect, useState } from "react"
import { Link, useNavigate, useParams } from "react-router"
import { toast } from "sonner"

import { Ago, Money } from "@/components/common"
import { PanelMenu, Tag } from "@/components/panel"
import { ErrorState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, ApiError } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { useParam } from "@/lib/filters"
import { duration } from "@/lib/format"
import { cn } from "@/lib/utils"
import { NotFound } from "@/pages/Status"
import { isActive, useLiveRun, useNow, useRun, useRunAction } from "../api"
import { Canvas, type RunOverlay } from "../Canvas"
import { TRIGGER } from "../kinds"
import { RunDialog } from "../RunDialog"
import type { RunDetail, RunEvent } from "../types"
import { RunBadge } from "../ui"
import { Ledger } from "./Ledger"
import { nodeOf, stepTitle, until, useOverlay } from "./lib"
import { Fact } from "./parts"
import { ApprovalDialog, ForkDialog, SkipDialog } from "./RunDialogs"
import { StepView } from "./StepDrawer"
import { StepPanel } from "./StepPanel"
import { StepsPanel } from "./Steps"

const ICON_ON_PHONE = "max-sm:w-7 max-sm:px-0"
const noop = () => {}
type Dialogs = "approve" | "skip" | "fork" | "rerun" | null

/** A run as a workspace: header actions, facts, banner, the graph by status, steps, the ledger and a step drawer. */
export function RunPage() {
  const { id = "", n = "" } = useParams()
  const q = useRun(id, n)
  const live = useLiveRun(q.data)
  const run = live.run
  const active = !!run && isActive(run)
  const now = useNow(active)
  const overlay = useOverlay(run)
  const [stepKey, setStep] = useParam("step")
  const [view, setView] = useParam("view")
  const [pane, setPane] = useParam("pane")
  const [follow, setFollow] = useState(true)
  const [ledger, setLedger] = useState(() => matchMedia("(min-width: 640px)").matches)
  const [dialog, setDialog] = useState<Dialogs>(null)
  const navigate = useNavigate()
  const confirm = useConfirm()
  const act = useRunAction(run?.id, (r) => {
    if (r.number !== run?.number) navigate(`/workflows/${id}/runs/${r.number}`)
    else live.follow()
  })
  // Follow keeps an open drawer on the step that is running.
  const current = run?.current_step
  useEffect(() => { if (follow && current && stepKey && stepKey !== current) setStep(current) }, [current]) // eslint-disable-line react-hooks/exhaustive-deps

  if (q.error instanceof ApiError && q.error.status === 404) return <NotFound />
  const crumbs = [{ label: "Workflows", to: "/workflows" }, { label: run?.workflow.name ?? "", to: `/workflows/${id}` }, { label: "Runs", to: `/workflows/${id}/runs` }, { label: `Run ${n}` }]
  if (!run || !overlay) return <Page crumbs={crumbs}>{q.isError && <ErrorState error={q.error} onRetry={() => void q.refetch()} />}</Page>

  const waitingStep = run.steps.find((s) => s.key === run.current_step)
  const approval = run.status === "waiting" && run.waiting_reason === "approval" && waitingStep
  const cancel = async () => {
    if (await confirm({ title: `Cancel run ${run.number}?`, action: "Cancel run", destructive: true })) act.mutate({ op: "cancel" })
  }
  const download = () => {
    const a = document.createElement("a")
    a.href = URL.createObjectURL(new Blob([JSON.stringify({ run, events: live.events }, null, 2)], { type: "application/json" }))
    a.download = `${run.workflow.name.toLowerCase().replace(/\W+/g, "-")}-run-${run.number}.json`
    a.click()
    URL.revokeObjectURL(a.href)
  }
  const verify = () => void api.get<{ ok: boolean; events: number }>(`/api/workflows/runs/${run.id}/verify`).then((v) => v.ok ? toast(`Verified ${v.events} events`) : toast.error("Chain broken"))
  const keys = run.steps.map((s) => s.key)
  const select = (key: string) => { setStep(key); if (key !== run.current_step) setFollow(false) }
  const steps = (
    <StepsPanel run={run} overlay={overlay} now={now} selected={stepKey || null} select={select} follow={follow} setFollow={setFollow}
      mode={view === "timeline" ? "timeline" : "list"} setMode={(m) => setView(m === "list" ? null : m)}
      className={cn("w-[380px] shrink-0 border-l bg-background max-lg:w-[320px] max-sm:w-full max-sm:border-l-0", pane === "graph" && "max-sm:hidden")} />
  )
  return (
    <Page
      crumbs={crumbs} flush className="flex flex-col overflow-hidden"
      actions={(
        <>
          <PanelMenu items={[
            { label: "Download JSON", icon: <Download />, onSelect: download },
            { label: "Verify ledger", icon: <ShieldCheck />, onSelect: verify },
            ...(!active ? [{ label: "Run again", icon: <Play />, onSelect: () => setDialog("rerun") }] : []),
            { label: "Run from here", icon: <FastForward />, onSelect: () => setDialog("fork") },
          ]} />
          {active && <Button variant="outline" size="sm" className={ICON_ON_PHONE} disabled={act.isPending} onClick={() => void cancel()}><Square /><span className="max-sm:sr-only">Cancel</span></Button>}
          {approval && <Button size="sm" className={ICON_ON_PHONE} onClick={() => setDialog("approve")}><Hourglass /><span className="max-sm:sr-only">Review approval</span></Button>}
          {(run.status === "failed" || run.status === "timed_out") && (
            <Button size="sm" className={ICON_ON_PHONE} disabled={act.isPending} onClick={() => act.mutate({ op: "redrive" })}><RotateCcw /><span className="max-sm:sr-only">Retry from failure</span></Button>
          )}
          {(run.status === "succeeded" || run.status === "cancelled") && <Button size="sm" className={ICON_ON_PHONE} onClick={() => setDialog("rerun")}><Play /><span className="max-sm:sr-only">Run again</span></Button>}
        </>
      )}
    >
      <FactsStrip run={run} now={now} />
      <Banner run={run} events={live.events} now={now} act={act} skip={() => setDialog("skip")} open={(k) => select(k)} />
      <div className="sm:hidden flex h-11 shrink-0 items-center border-b px-4">
        <ToggleGroup type="single" variant="segmented" size="sm" value={pane === "graph" ? "graph" : "steps"} onValueChange={(v) => v && setPane(v === "steps" ? null : v)} className="w-full [&>*]:flex-1">
          <ToggleGroupItem value="steps" className="text-xs">Steps</ToggleGroupItem>
          <ToggleGroupItem value="graph" className="text-xs">Graph</ToggleGroupItem>
        </ToggleGroup>
      </div>
      <div className="relative flex min-h-0 flex-1">
        <div className={cn("relative min-w-0 flex-1 bg-muted/40", pane !== "graph" && "max-sm:hidden")}>
          <ReactFlowProvider><RunGraph run={run} overlay={overlay} panel={!!stepKey} selected={stepKey ? nodeOf(stepKey) : null} select={(node) => { const k = overlay.nodes[node]?.key; if (k) select(k) }} /></ReactFlowProvider>
        </div>
        {steps}
        {stepKey && (
          <StepPanel title={`Run ${run.number}`} index={keys.indexOf(stepKey)} count={keys.includes(stepKey) ? keys.length : 0} close={() => setStep(null)}
            step={(d) => { const k = keys[keys.indexOf(stepKey) + d]; if (k) select(k) }}>
            <StepView run={run} events={live.events} stepKey={stepKey} now={now} />
          </StepPanel>
        )}
      </div>
      <Ledger run={run} events={live.events} fresh={live.fresh} select={select} open={ledger} setOpen={setLedger} />
      {approval && <ApprovalDialog run={run} stepKey={waitingStep.key} open={dialog === "approve"} onOpenChange={(o) => setDialog(o ? "approve" : null)} onDone={live.follow} />}
      {dialog === "skip" && <SkipDialog run={run} open onOpenChange={(o) => !o && setDialog(null)} onDone={live.follow} />}
      {dialog === "fork" && <ForkDialog run={run} step={stepKey || undefined} open onOpenChange={(o) => !o && setDialog(null)} />}
      {dialog === "rerun" && <RunDialog id={id} spec={run.spec} initial={run.inputs} open onOpenChange={(o) => !o && setDialog(null)} />}
    </Page>
  )
}

export function RunGraph({ run, overlay, selected, select, panel = false }: { run: RunDetail; overlay: RunOverlay; selected: string | null; select: (node: string) => void; panel?: boolean }) {
  const [none] = useState(() => new Set<string>())
  return (
    <Canvas spec={run.spec} changed={none} flash={none} selected={selected} ro select={(n) => n && select(n)} add={noop} remove={noop} duplicate={noop} panel={panel} overlap={64} run={overlay} />
  )
}

function FactsStrip({ run, now }: { run: RunDetail; now: number }) {
  const T = TRIGGER[run.trigger.kind]
  const took = run.duration_ms ?? (run.ended_at ? Date.parse(run.ended_at) : now) - Date.parse(run.started_at)
  return (
    <div className="flex h-11 shrink-0 items-center gap-5 overflow-x-auto border-b [&>*]:shrink-0 px-4 text-sm [scrollbar-width:none] lg:px-6">
      <RunBadge status={run.status} />
      {run.test && <Tag>Test</Tag>}
      <Fact label="Trigger"><T.icon className="size-3.5 text-muted-foreground" />{run.triggered_by?.name ?? T.label}</Fact>
      <Fact label="Version"><Link to={`/workflows/${run.workflow.id}/versions`} className="tabular hover:underline">{run.test ? "Draft" : `v${run.version}`}</Link></Fact>
      <Fact label="Started"><Ago iso={run.started_at} /></Fact>
      <Fact label="Duration"><span className="tabular">{duration(took)}</span></Fact>
      <Fact label="Cost"><Money usd={run.cost_usd} />{run.budget_usd > 0 && <span className="text-muted-foreground tabular">of <Money usd={run.budget_usd} /></span>}</Fact>
      {run.label && <Fact label="Label">{run.label}</Fact>}
      {run.parent && <Fact label="From"><Link to={`/workflows/${run.workflow.id}/runs/${run.parent.number}`} className="tabular hover:underline">Run {run.parent.number}</Link></Fact>}
    </div>
  )
}

type Act = ReturnType<typeof useRunAction>

function Banner({ run, events, now, act, skip, open }: { run: RunDetail; events: RunEvent[]; now: number; act: Act; skip: () => void; open: (k: string) => void }) {
  const s = run.steps.find((x) => x.key === (run.current_step ?? run.failure?.step))
  const title = s && stepTitle(run.spec, s)
  const at = s && <button className="font-medium hover:underline" onClick={() => open(s.key)}>{title}</button>
  if (run.status === "waiting" && run.waiting_reason === "unknown") {
    return (
      <Strip tone="destructive" icon={CircleHelp}
        actions={<>
          <Button variant="ghost" size="sm" disabled={act.isPending} onClick={() => act.mutate({ op: "decide", body: { decision: "fail" } })}><X />Fail</Button>
          <Button variant="outline" size="sm" disabled={act.isPending} onClick={skip}>Skip with output</Button>
          <Button size="sm" disabled={act.isPending} onClick={() => act.mutate({ op: "decide", body: { decision: "retry" } })}><RotateCcw />Retry</Button>
        </>}>
        <span>Outcome unknown at {at}</span>
        {s?.reason && <span className="truncate text-muted-foreground">{s.reason}</span>}
      </Strip>
    )
  }
  if (run.status === "waiting" && s?.wait?.status === "waiting") {
    const who = s.wait.addressees.map((p) => p.name).join(", ")
    return (
      <Strip tone="warning" icon={Hourglass}>
        <span>{s.wait.kind === "approval" ? "Waiting for approval" : s.wait.kind === "question" ? "Waiting for an answer" : "Waiting"} at {at}</span>
        {who && <Fact label={s.wait.kind === "approval" ? "Approvers" : "Asked"}>{who}</Fact>}
        {s.wait.sent[0] && <Fact label="Sent">{s.wait.sent[0].where}</Fact>}
        {s.wait.expires_at && <Fact label="Expires in"><span className="tabular">{until(s.wait.expires_at, now)}</span></Fact>}
      </Strip>
    )
  }
  if (run.status === "failed" || run.status === "timed_out") {
    return (
      <Strip tone="destructive" icon={CircleX}>
        <span>{run.status === "timed_out" ? "Timed out" : "Failed"}{at && <> at {at}</>}</span>
        {run.failure?.message && <span className="min-w-0 truncate text-muted-foreground">{run.failure.message}</span>}
      </Strip>
    )
  }
  if (run.status === "cancelled") {
    const by = events.findLast((e) => e.type === "run.cancelled")?.actor
    return <Strip tone="muted" icon={Square}><span>Cancelled</span>{by && <Fact label="By">{by}</Fact>}</Strip>
  }
  if (run.outcome === "nothing_to_do") return <Strip tone="muted" icon={ShieldCheck}><span>Nothing to do</span></Strip>
  return null
}

const TONE = {
  warning: "bg-warning/10 [&>svg]:text-warning", destructive: "bg-destructive/8 [&>svg]:text-destructive", muted: "bg-muted [&>svg]:text-muted-foreground",
}

function Strip({ tone, icon: I, children, actions }: { tone: keyof typeof TONE; icon: React.ComponentType<{ className?: string }>; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className={cn("flex min-h-11 shrink-0 items-center gap-3 border-b px-4 py-1.5 text-sm animate-in fade-in lg:px-6 max-sm:flex-wrap", TONE[tone])}>
      <I className="size-4 shrink-0" />
      <div className="flex min-w-0 flex-1 items-center gap-4 max-sm:flex-wrap max-sm:gap-x-3 max-sm:gap-y-0.5">{children}</div>
      {actions && <div className="flex shrink-0 items-center gap-2 max-sm:w-full max-sm:[&>*]:flex-1">{actions}</div>}
    </div>
  )
}

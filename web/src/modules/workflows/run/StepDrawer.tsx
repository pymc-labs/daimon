import { CircleCheck, CircleDot, Circle, FileText, MessageSquare, StickyNote, Wrench } from "lucide-react"

import { Ago, Mono, Money } from "@/components/common"
import { DrawerTabs, Facts, PanelHead, PanelSection, Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useParam } from "@/lib/filters"
import { duration } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useStep } from "../api"
import { KIND, STEP } from "../kinds"
import type { Proposal, RunDetail, RunEvent, StepDetail, StepRun } from "../types"
import { clock, stepTitle, until } from "./lib"
import { JsonTree, StepIcon } from "./parts"

const PROPOSAL: Record<Proposal["status"], string> = { proposed: "Proposed", approved: "Approved", rejected: "Rejected", sent: "Sent", would_send: "Would send" }
const kb = (n: number) => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`)

/** One step of a run: attempt picker, facts and the Summary, Input, Output, Activity and Events tabs. */
export function StepView({ run, events, stepKey, now }: { run: RunDetail; events: RunEvent[]; stepKey: string; now: number }) {
  const s = run.steps.find((x) => x.key === stepKey)
  const [attempt, setAttempt] = useParam("attempt")
  const n = attempt ? Number(attempt) : null
  const live = s?.status === "running" || s?.status === "waiting"
  const q = useStep(run.id, s ? stepKey : null, n, live)
  if (!s) return <PanelSection>Not reached</PanelSection>
  const node = run.spec.nodes[s.node]
  const K = node ? KIND[node.kind] : null
  const a = s.attempts.find((x) => x.n === n) ?? s.attempts.at(-1)
  const took = a?.duration_ms ?? (s.started_at ? (s.ended_at ? Date.parse(s.ended_at) : now) - Date.parse(s.started_at) : null)
  const mine = events.filter((e) => e.step === s.key)
  return (
    <>
      <PanelHead
        icon={K && <K.icon className={cn("size-4", K.text)} />}
        title={stepTitle(run.spec, s)}
        subtitle={<span className="flex items-center gap-1.5"><StepIcon status={s.status} /><span className="text-foreground">{STEP[s.status].label}</span></span>}
        tags={<>{s.visit > 1 && <Tag>Visit {s.visit}</Tag>}{s.item && <Tag>Item {s.item.index + 1}</Tag>}{s.reused && <Tag>{s.reused === "pinned" ? "Pinned" : "Reused"}</Tag>}</>}
        actions={s.attempts.length > 1 && (
          <Select value={String(a?.n)} onValueChange={(v) => setAttempt(Number(v) === s.attempts.length ? null : v)}>
            <SelectTrigger size="sm" className="h-8 w-40"><SelectValue /></SelectTrigger>
            <SelectContent align="end">
              {s.attempts.map((x) => (
                <SelectItem key={x.n} value={String(x.n)}>
                  <StepIcon status={x.status === "abandoned" ? "unknown" : x.status} />Attempt {x.n} of {s.attempts.length}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      />
      {s.reason && (
        <div className="mx-5 mb-4 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm">
          {a?.error && <div className="text-xs text-destructive">{a.error.class}</div>}
          {s.reason}
        </div>
      )}
      <PanelSection>
        <Facts rows={[
          ["Agent", q.data?.agent],
          ["Model", q.data?.model],
          ["Started", s.started_at && <Ago key="a" iso={s.started_at} />],
          ["Duration", took != null && <span key="d" className="tabular">{duration(took)}</span>],
          ["Cost", (a?.cost_usd ?? s.cost_usd) > 0 && <Money key="c" usd={a?.cost_usd ?? s.cost_usd} />],
          ["Session", a?.session && <Mono key="s">{a.session}</Mono>],
          ...waitFacts(s, now),
        ]} />
      </PanelSection>
      <div className="border-t">
        <QueryState query={q}>
          {(d) => (
            <DrawerTabs tabs={[
              { value: "summary", label: "Summary", children: <Summary run={run} s={s} d={d} /> },
              { value: "input", label: "Input", children: <PanelSection className="border-t-0"><JsonTree value={d.input} root="input" untrusted={d.untrusted} /></PanelSection> },
              { value: "output", label: "Output", children: <Output d={d} /> },
              { value: "activity", label: "Activity", hidden: !d.activity.length && !d.plan.length, children: <Activity d={d} /> },
              { value: "events", label: "Events", children: <Events events={mine} /> },
            ]} />
          )}
        </QueryState>
      </div>
    </>
  )
}

function waitFacts(s: StepRun, now: number): [string, React.ReactNode][] {
  const w = s.wait
  if (!w) return []
  return [
    [w.kind === "approval" ? "Approvers" : "Asked", w.addressees.map((p) => p.name).join(", ")],
    ["Expires", w.status === "waiting" && w.expires_at && <span key="e" className="text-warning tabular">In {until(w.expires_at, now)}</span>],
    ["Decision", w.decision && <span key="x" className="capitalize">{w.decision.replace(/_/g, " ")}</span>],
    ["Decided by", w.decider?.name],
  ]
}

function Summary({ run, s, d }: { run: RunDetail; s: StepRun; d: StepDetail }) {
  const items = run.steps.filter((x) => x.item && x.key.startsWith(`${s.key}[`))
  const [, setStep] = useParam("step")
  return (
    <>
      <PanelSection className="border-t-0">
        {d.summary ? <p className="text-sm whitespace-pre-wrap">{d.summary}</p> : <p className="text-sm text-faint">No summary</p>}
      </PanelSection>
      {d.route && (
        <PanelSection title="Route">
          <Facts rows={[["Chosen", <Tag key="r">{d.route.name}</Tag>], ["Reason", d.route.reason]]} />
        </PanelSection>
      )}
      {items.length > 0 && (
        <PanelSection title={`Items ${s.progress ? `${s.progress.done} of ${s.progress.total}` : ""}`}>
          <div className="-mx-2">
            {items.map((i) => (
              <button key={i.key} onClick={() => setStep(i.key)} className="flex h-8 w-full items-center gap-2 rounded-md px-2 text-left text-sm hover:bg-accent">
                <StepIcon status={i.status} /><span className="min-w-0 flex-1 truncate">{stepTitle(run.spec, i)}</span>
                {i.cost_usd > 0 && <Money usd={i.cost_usd} className="text-xs text-muted-foreground" />}
              </button>
            ))}
          </div>
        </PanelSection>
      )}
      {d.files.length > 0 && (
        <PanelSection title="Files">
          {d.files.map((f) => (
            <div key={f.name} className="flex h-8 items-center gap-2 text-sm"><FileText className="size-4 text-muted-foreground" /><span className="flex-1 truncate">{f.name}</span><span className="text-xs text-muted-foreground tabular">{kb(f.size)}</span></div>
          ))}
        </PanelSection>
      )}
    </>
  )
}

function Output({ d }: { d: StepDetail }) {
  return (
    <>
      <PanelSection className="border-t-0"><JsonTree value={d.output} root="output" untrusted={d.untrusted} /></PanelSection>
      {d.proposals.length > 0 && (
        <PanelSection title="Proposals">
          {d.proposals.map((p) => (
            <div key={p.id} className="flex min-h-8 items-center gap-3 text-sm">
              <Mono className="shrink-0 text-xs">{p.tool}</Mono><span className="min-w-0 flex-1 truncate">{p.summary}</span><Tag>{PROPOSAL[p.status]}</Tag>
            </div>
          ))}
        </PanelSection>
      )}
      {d.rejections.length > 0 && (
        <PanelSection title="Rejected outputs">
          {d.rejections.map((r) => (
            <div key={r.attempt} className="mb-2 text-sm last:mb-0">
              <div className="text-xs text-muted-foreground">Attempt {r.attempt}</div>
              <ul className="mt-1 space-y-0.5">{r.errors.map((e) => <li key={e}><Mono className="text-xs text-destructive">{e}</Mono></li>)}</ul>
            </div>
          ))}
        </PanelSection>
      )}
    </>
  )
}

const LINE = { message: MessageSquare, tool: Wrench, note: StickyNote }
const PLAN = { done: CircleCheck, active: CircleDot, todo: Circle }

function Activity({ d }: { d: StepDetail }) {
  return (
    <>
      {d.plan.length > 0 && (
        <PanelSection title="Plan" className="border-t-0">
          {d.plan.map((p) => {
            const I = PLAN[p.status]
            return <div key={p.text} className="flex h-7 items-center gap-2 text-sm"><I className={cn("size-3.5", p.status === "done" ? "text-success" : p.status === "active" ? "text-link" : "text-faint")} /><span className={cn(p.status === "todo" && "text-muted-foreground")}>{p.text}</span></div>
          })}
        </PanelSection>
      )}
      <PanelSection title="Activity" className={cn(!d.plan.length && "border-t-0")}>
        {d.activity.map((l, i) => {
          const I = LINE[l.kind]
          return (
            <div key={i} className="grid grid-cols-[64px_16px_1fr] items-start gap-2 py-1 text-sm">
              <span className="text-xs text-muted-foreground tabular">{clock(l.at)}</span><I className="mt-0.5 size-3.5 text-muted-foreground" />
              <span className="min-w-0">{l.tool && <Mono className="mr-1.5 text-xs">{l.tool}</Mono>}{l.text}</span>
            </div>
          )
        })}
      </PanelSection>
    </>
  )
}

function Events({ events }: { events: RunEvent[] }) {
  return (
    <div className="py-2 text-xs">
      {events.map((e) => (
        <div key={e.seq} className="grid grid-cols-[36px_64px_minmax(0,150px)_1fr] items-center gap-2 px-5 py-1">
          <span className="text-faint tabular">{e.seq}</span><span className="text-muted-foreground tabular">{clock(e.at)}</span>
          <Mono className="truncate">{e.type}</Mono><span className="min-w-0 truncate">{e.detail}</span>
        </div>
      ))}
    </div>
  )
}

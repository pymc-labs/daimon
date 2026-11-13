import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"

import { Facts } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { useRunAction, useWorkflow } from "../api"
import { titleOf } from "../spec"
import type { RunDetail } from "../types"
import { choice, stepTitle, until } from "./lib"
import { StepIcon } from "./parts"

type Open = { open: boolean; onOpenChange: (o: boolean) => void }

function Shell({ title, open, onOpenChange, children, footer, error, wide }: Open & {
  title: string; children: React.ReactNode; footer: React.ReactNode; error?: string | null; wide?: boolean
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={cn("gap-0 p-0", wide ? "sm:max-w-lg" : "sm:max-w-md")}>
        <DialogHeader className="border-b px-5 py-4"><DialogTitle>{title}</DialogTitle></DialogHeader>
        <div className="space-y-4 px-5 py-4">{children}</div>
        <DialogFooter className="m-0 flex-col items-stretch gap-2 border-t px-5 py-3 sm:flex-col sm:justify-start">
          <div className="flex justify-end gap-2 max-sm:[&>*]:flex-1">{footer}</div>
          {error && <p className="text-right text-sm text-destructive">{error}</p>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Decide a waiting approval from the run page: one button per route, Approve filled. */
export function ApprovalDialog({ run, stepKey, open, onOpenChange, onDone }: Open & { run: RunDetail; stepKey: string; onDone: () => void }) {
  const s = run.steps.find((x) => x.key === stepKey)
  const act = useRunAction(run.id, () => { onOpenChange(false); onDone() })
  const node = s && run.spec.nodes[s.node]
  const routes = Object.keys(node?.routes ?? { approve: "", reject: "" })
  const main = routes.includes("approve") ? "approve" : routes[0]
  const go = (route: string) => act.mutate({ op: "approve", body: { step: stepKey, route } }, { onSuccess: () => toast(`${cap(route)}d`) })
  return (
    <Shell title="Review approval" open={open} onOpenChange={onOpenChange} error={act.error?.message}
      footer={<>
        <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
        {routes.filter((r) => r !== main).map((r) => <Button key={r} variant="outline" disabled={act.isPending} onClick={() => go(r)}>{cap(r)}</Button>)}
        {main && <Button disabled={act.isPending} onClick={() => go(main)}>{cap(main)}</Button>}
      </>}>
      {s && (
        <Facts rows={[
          ["Step", <span key="s" className="flex items-center gap-1.5"><StepIcon status={s.status} />{stepTitle(run.spec, s)}</span>],
          ["Approvers", s.wait?.addressees.map((p) => p.name).join(", ")],
          ["Expires", s.wait?.expires_at && `In ${until(s.wait.expires_at)}`],
          ["Shows", node?.show && <code key="c" className="font-mono text-xs">{node.show}</code>],
        ]} />
      )}
    </Shell>
  )
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ")

/** Skip an unknown step with the output it should have produced. */
export function SkipDialog({ run, open, onOpenChange, onDone }: Open & { run: RunDetail; onDone: () => void }) {
  const [text, setText] = useState("{\n  \n}")
  const [bad, setBad] = useState<string | null>(null)
  const act = useRunAction(run.id, () => { onOpenChange(false); onDone(); toast("Skipped with output") })
  const go = () => {
    try { act.mutate({ op: "decide", body: { decision: "skip", output: JSON.parse(text) } }); setBad(null) } catch { setBad("Not valid JSON") }
  }
  const s = run.steps.find((x) => x.key === run.current_step)
  return (
    <Shell title="Skip with output" open={open} onOpenChange={onOpenChange} error={bad ?? act.error?.message}
      footer={<><Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button><Button disabled={act.isPending} onClick={go}>Skip step</Button></>}>
      {s && <Facts rows={[["Step", stepTitle(run.spec, s)], ["Reason", s.reason]]} />}
      <div className="space-y-1.5">
        <Label htmlFor="skip-out">Output</Label>
        <Textarea id="skip-out" value={text} onChange={(e) => setText(e.target.value)} rows={6} className="font-mono text-xs" aria-invalid={!!bad} />
      </div>
    </Shell>
  )
}

type Version = "same" | "live" | "draft"

/** Run from here: a new run that reuses the finished steps before `step` and runs the rest on the chosen version. */
export function ForkDialog({ run, step: initial, version: initialVersion = "same", open, onOpenChange }: Open & { run: RunDetail; step?: string; version?: Version }) {
  const wf = useWorkflow(run.workflow.id).data
  const navigate = useNavigate()
  const tops = run.steps.filter((s) => !s.item && s.status !== "pending")
  const [step, setStep] = useState(initial ?? run.failure?.step ?? tops.at(-1)?.key ?? run.spec.start)
  const [version, setVersion] = useState<Version>(initialVersion)
  const [vals, setVals] = useState<Record<string, string>>(() => Object.fromEntries(Object.entries(run.inputs).map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)])))
  const act = useRunAction(run.id, (r) => { onOpenChange(false); toast(`Run ${r.number} started`); navigate(`/workflows/${r.workflow.id}/runs/${r.number}`) })
  const i = tops.findIndex((s) => s.key === step)
  const reused = i < 0 ? 0 : tops.slice(0, i).filter((s) => s.status === "succeeded").length
  const versions: { id: Version; label: string; fact: string; off?: boolean }[] = [
    { id: "same", label: "This run", fact: `Version ${run.version}` },
    { id: "live", label: "Live version", fact: wf?.live_version ? `Version ${wf.live_version}` : "None", off: !wf?.live_version },
    { id: "draft", label: "Draft", fact: wf?.draft_revision ? `${wf.draft_changes} changes` : "None", off: !wf?.draft_revision },
  ]
  const props = Object.keys(run.spec.inputs?.properties ?? run.inputs)
  return (
    <Shell title="Run from here" open={open} onOpenChange={onOpenChange} error={act.error?.message} wide
      footer={<>
        <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button disabled={act.isPending} onClick={() => act.mutate({ op: "fork", body: { step, inputs: vals, version } })}>Start run</Button>
      </>}>
      <div className="space-y-1.5">
        <Label>Start at</Label>
        <Select value={step} onValueChange={setStep}>
          <SelectTrigger className="h-8 w-full"><SelectValue /></SelectTrigger>
          <SelectContent>
            {tops.map((s) => <SelectItem key={s.key} value={s.key}><StepIcon status={s.status} />{stepTitle(run.spec, s)}{s.visit > 1 && ` (visit ${s.visit})`}</SelectItem>)}
            {!tops.length && <SelectItem value={run.spec.start}>{titleOf(run.spec.start, run.spec.nodes[run.spec.start])}</SelectItem>}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground tabular">Reuses {reused} {reused === 1 ? "step" : "steps"}</p>
      </div>
      <div className="space-y-1.5">
        <Label>Version</Label>
        <div className="grid gap-2 sm:grid-cols-3">
          {versions.map((v) => (
            <button key={v.id} type="button" disabled={v.off} aria-pressed={version === v.id} onClick={() => setVersion(v.id)}
              className={cn(choice(version === v.id), "disabled:opacity-50 disabled:hover:border-border")}>
              <span className="text-sm font-medium">{v.label}</span>
              <span className="text-xs text-muted-foreground">{v.fact}</span>
            </button>
          ))}
        </div>
      </div>
      {props.length > 0 && (
        <div className="space-y-3">
          {props.map((k) => (
            <div key={k} className="space-y-1.5">
              <Label htmlFor={`fork-${k}`}>{run.spec.inputs?.properties?.[k]?.title ?? k}</Label>
              <Input id={`fork-${k}`} value={vals[k] ?? ""} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} className="h-8" />
            </div>
          ))}
        </div>
      )}
    </Shell>
  )
}

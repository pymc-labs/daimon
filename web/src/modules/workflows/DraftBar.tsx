import { CircleAlert, CircleCheck, FlaskConical, KeyRound, Minus, Pencil, Plus, Sparkles, TriangleAlert } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { useConfirm } from "@/lib/confirm"
import { cn } from "@/lib/utils"
import { discardDraft, usePublish } from "./api"
import { PANEL_W } from "./Canvas"
import { TestRunSheet } from "./run/TestRun"
import { titleOf } from "./spec"
import type { Draft, GrantKind, Problem, Workflow } from "./types"
import { KIND } from "./kinds"
import { Dot } from "./ui"

const GRANT: Record<GrantKind, string> = {
  unattended_write: "Writes without approval", builtins: "Built-in tools", code_network: "Code with network", unverified_webhook: "Unverified webhook",
  budget_over_default: "Budget over default", untrusted_waiver: "Untrusted input waiver",
}
const EFFECT = { added: { icon: Plus, cls: "text-success" }, changed: { icon: Pencil, cls: "text-muted-foreground" }, removed: { icon: Minus, cls: "text-destructive" } }

function ProblemRow({ p, draft, onSelect }: { p: Problem; draft: Draft; onSelect?: () => void }) {
  const Icon = p.severity === "error" ? CircleAlert : TriangleAlert
  const n = p.node ? draft.spec.nodes[p.node] : undefined
  return (
    <button className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent disabled:hover:bg-transparent" onClick={onSelect} disabled={!onSelect}>
      <Icon className={cn("mt-0.5 size-3.5 shrink-0", p.severity === "error" ? "text-destructive" : "text-warning")} />
      <span className="min-w-0 flex-1">{p.message}</span>
      {n && <span className="shrink-0 text-xs text-muted-foreground">{titleOf(p.node!, n)}</span>}
    </button>
  )
}

/** Ask Daimon and Test run while the draft matches the live version, so the draft bar can stay hidden (the agent editor's pattern). */
export function DraftTools({ wf, draft, ask }: { wf: Workflow; draft: Draft; ask: () => void }) {
  const [test, setTest] = useState(false)
  return (
    <>
      <Button variant="outline" size="sm" className="bg-popover shadow-xs" onClick={ask}><Sparkles />Ask Daimon</Button>
      <Button variant="outline" size="sm" className="bg-popover shadow-xs" onClick={() => setTest(true)}><FlaskConical />Test run</Button>
      <TestRunSheet wf={wf} draft={draft} open={test} onOpenChange={setTest} />
    </>
  )
}

export function DraftBar({ wf, draft, shift, select, ask, adopt }: {
  wf: Workflow; draft: Draft; shift: boolean; select: (id: string) => void; ask: () => void; adopt: (d: Draft) => void
}) {
  const [review, setReview] = useState(false)
  const [test, setTest] = useState(false)
  const [probs, setProbs] = useState(false)
  const confirm = useConfirm()
  const n = draft.changes.length
  if (!n) return null
  const problems = [...draft.validation.errors, ...draft.validation.warnings]
  const discard = async () => {
    if (!(await confirm({ title: "Discard draft?", action: "Discard", destructive: true }))) return
    adopt(await discardDraft(wf.id))
    toast("Draft discarded")
  }
  return (
    <>
      <div
        className="absolute bottom-4 z-10 flex h-11 -translate-x-1/2 items-center gap-3 rounded-xl border bg-popover pr-1.5 pl-4 whitespace-nowrap max-sm:gap-1.5 max-sm:pl-3 shadow-[0_8px_28px_rgb(12_31_64/0.12)] transition-[left]"
        style={{ left: shift ? `calc((100% - ${PANEL_W}px) / 2)` : "50%" }}
      >
        <span className="flex items-center gap-2 text-sm"><Dot /><span className="font-medium tabular">{n}<span className="max-sm:sr-only"> {n === 1 ? "change" : "changes"}</span></span></span>
        <Popover open={probs} onOpenChange={setProbs}>
          <PopoverTrigger asChild>
            <button className={cn("flex h-7 items-center gap-1.5 rounded-md px-2 text-sm tabular hover:bg-accent", problems.length ? draft.validation.errors.length ? "text-destructive" : "text-warning" : "text-muted-foreground")}>
              {problems.length ? <CircleAlert className="size-3.5" /> : <CircleCheck className="size-3.5 text-success" />}
              <span className={cn(!problems.length && "text-muted-foreground")}>{problems.length ? <>{problems.length}<span className="max-sm:sr-only"> {problems.length === 1 ? "problem" : "problems"}</span></> : "No problems"}</span>
            </button>
          </PopoverTrigger>
          {problems.length > 0 && (
            <PopoverContent side="top" align="start" sideOffset={10} className="w-96 p-1.5">
              {problems.map((p, i) => <ProblemRow key={i} p={p} draft={draft} onSelect={p.node ? () => { setProbs(false); select(p.node!) } : undefined} />)}
            </PopoverContent>
          )}
        </Popover>
        <span className="h-5 w-px bg-border" />
        <span className="flex items-center gap-1">
          <Button variant="ghost" size="sm" onClick={ask} aria-label="Ask Daimon"><Sparkles /><span className="max-sm:hidden">Ask Daimon</span></Button>
          <Button variant="ghost" size="sm" onClick={() => void discard()}>Discard</Button>
          <Button variant="outline" size="sm" onClick={() => setTest(true)}>Test run</Button>
          <Button size="sm" onClick={() => setReview(true)}>Review</Button>
        </span>
      </div>
      <TestRunSheet wf={wf} draft={draft} open={test} onOpenChange={setTest} />
      <Review wf={wf} draft={draft} open={review} onOpenChange={setReview} />
    </>
  )
}

function Review({ wf, draft, open, onOpenChange }: { wf: Workflow; draft: Draft; open: boolean; onOpenChange: (o: boolean) => void }) {
  const [note, setNote] = useState("")
  const publish = usePublish(wf.id)
  const errors = draft.validation.errors
  const next = (wf.live_version ?? 0) + 1
  const label = (node: string | null, field?: string) => {
    if (!node) return <span className="font-medium">{field ? field.charAt(0).toUpperCase() + field.slice(1) : "Workflow"}</span>
    const s = draft.spec.nodes[node]
    const K = s ? KIND[s.kind] : null
    return <span className="flex items-center gap-1.5 font-medium">{K && <K.icon className={cn("size-3.5", K.text)} />}{titleOf(node, s)}</span>
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="gap-0 p-0 sm:max-w-xl">
        <DialogHeader className="border-b px-5 py-4"><DialogTitle>Publish v{next}</DialogTitle></DialogHeader>
        <div className="max-h-[60vh] space-y-5 overflow-y-auto px-5 py-4">
          <section>
            <h3 className="mb-2 text-xs font-medium text-muted-foreground">Changes</h3>
            {draft.changes.map((c, i) => {
              const E = EFFECT[c.effect]
              return <div key={i} className="flex h-8 items-center gap-2 text-sm"><E.icon className={cn("size-3.5", E.cls)} />{label(c.node, c.field)}<span className="ml-auto text-xs text-muted-foreground capitalize">{c.effect}</span></div>
            })}
          </section>
          {draft.validation.grants.length > 0 && (
            <section>
              <h3 className="mb-2 text-xs font-medium text-muted-foreground">Grants</h3>
              {draft.validation.grants.map((g, i) => (
                <div key={i} className="flex h-8 items-center gap-2 text-sm">
                  <KeyRound className="size-3.5 text-warning" /><span className="font-medium">{GRANT[g.kind]}</span>
                  <span className="ml-auto truncate text-xs text-muted-foreground">{g.node ? titleOf(g.node, draft.spec.nodes[g.node]) : g.detail}</span>
                </div>
              ))}
            </section>
          )}
          {errors.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-medium text-muted-foreground">Problems</h3>
              <div className="-mx-2">{errors.map((p, i) => <ProblemRow key={i} p={p} draft={draft} />)}</div>
            </section>
          )}
        </div>
        <DialogFooter className="m-0 flex-col items-stretch gap-2 border-t px-5 py-3 sm:flex-col sm:justify-start">
          <div className="flex items-center gap-2 max-sm:flex-col max-sm:items-stretch">
            <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note" className="h-8 sm:flex-1" />
            <div className="flex gap-2 max-sm:[&>*]:flex-1">
              <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button disabled={errors.length > 0 || publish.isPending} onClick={() => publish.mutate(note, { onSuccess: () => onOpenChange(false) })}>Publish</Button>
            </div>
          </div>
          {errors.length > 0 && <p className="text-right text-sm text-destructive">{errors.length === 1 ? "Fix 1 problem to publish" : `Fix ${errors.length} problems to publish`}</p>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

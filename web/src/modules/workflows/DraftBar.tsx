import { CircleAlert, CircleCheck, FlaskConical, Sparkles, TriangleAlert } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { useConfirm } from "@/lib/confirm"
import { cn } from "@/lib/utils"
import { discardDraft } from "./api"
import { PANEL_W } from "./Canvas"
import { ReviewDialog } from "./Review"
import { TestRunSheet } from "./run/TestRun"
import { titleOf } from "./spec"
import type { Draft, Problem, Workflow } from "./types"
import { Dot } from "./ui"

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
      <ReviewDialog wf={wf} draft={draft} open={review} onOpenChange={setReview} />
    </>
  )
}

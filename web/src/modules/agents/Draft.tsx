import { ArrowRight, ChevronRight, Minus, Pencil, Plus, TriangleAlert } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { useApply, useDiscard } from "./api"
import { Diff, EffectTag } from "./bits"
import { EFFECT } from "./lib"
import type { Agent, Change, Draft, Effect } from "./types"

const KIND = { add: Plus, remove: Minus, edit: Pencil }
const ORDER: Effect[] = ["session", "threads", "live"]

function ChangeRow({ c, showEffect }: { c: Change; showEffect: boolean }) {
  const [open, setOpen] = useState(false)
  const Icon = KIND[c.kind]
  return (
    <div>
      <div className={cn("flex min-h-8 items-center gap-2 text-sm", c.text && "cursor-pointer")} onClick={() => c.text && setOpen(!open)}>
        <span className={cn("flex size-4 shrink-0 items-center justify-center rounded", c.kind === "add" ? "text-success" : c.kind === "remove" ? "text-destructive" : "text-muted-foreground")}>
          <Icon className="size-3.5" />
        </span>
        <span className="shrink-0 font-medium">{c.label}</span>
        {!c.text && (c.before || c.after) && (
          <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
            {c.before && <span className="truncate line-through decoration-faint">{c.before}</span>}
            {c.before && c.after && <ArrowRight className="size-3 shrink-0 text-faint" />}
            {c.after && <span className="truncate text-foreground/80">{c.after}</span>}
          </span>
        )}
        {c.text && <span className="flex items-center gap-0.5 text-xs text-link">Diff<ChevronRight className={cn("size-3 transition-transform", open && "rotate-90")} /></span>}
        {showEffect && <EffectTag effect={c.effect} className="ml-auto" />}
      </div>
      {open && c.before != null && c.after != null && <Diff before={c.before} after={c.after} className="mt-1 mb-2 max-h-72 overflow-y-auto" />}
    </div>
  )
}

export function ChangeList({ changes, showEffect = true }: { changes: Change[]; showEffect?: boolean }) {
  return <div className="space-y-0.5">{changes.map((c) => <ChangeRow key={c.key} c={c} showEffect={showEffect} />)}</div>
}

export function DraftBar({ agent, draft, shift }: { agent: Agent; draft: Draft; shift: boolean }) {
  const [review, setReview] = useState(false)
  const discard = useDiscard(agent.id)
  const n = draft.changes.length
  if (!n) return null
  const counts = ORDER.map((e) => [e, draft.changes.filter((c) => c.effect === e).length] as const).filter(([, k]) => k)
  return (
    <>
      <div
        className="absolute bottom-4 z-10 flex h-11 -translate-x-1/2 items-center gap-3 rounded-xl border bg-popover pr-1.5 pl-4 shadow-[0_8px_28px_rgb(12_31_64/0.12)] transition-[left]"
        style={{ left: shift ? "calc((100% - 440px) / 2)" : "50%" }}
      >
        <span className="size-1.5 rounded-full bg-link" />
        <span className="text-sm font-medium whitespace-nowrap tabular">{n} {n === 1 ? "change" : "changes"}</span>
        <span className="flex items-center gap-1">{counts.map(([e, k]) => <span key={e} className={cn("inline-flex h-5 items-center rounded px-1.5 text-[11px] font-medium tabular", EFFECT[e].cls)}>{k} {EFFECT[e].label}</span>)}</span>
        {draft.stale && <span className="flex items-center gap-1 text-xs text-warning"><TriangleAlert className="size-3.5" />v{agent.version} is newer</span>}
        <span className="h-5 w-px bg-border" />
        <Button variant="ghost" size="sm" onClick={() => discard.mutate()} disabled={discard.isPending}>Discard</Button>
        <Button size="sm" onClick={() => setReview(true)}>Review</Button>
      </div>
      <Review open={review} onOpenChange={setReview} agent={agent} draft={draft} />
    </>
  )
}

function Review({ open, onOpenChange, agent, draft }: { open: boolean; onOpenChange: (o: boolean) => void; agent: Agent; draft: Draft }) {
  const [note, setNote] = useState("")
  const apply = useApply(agent.id)
  const groups = ORDER.map((e) => ({ e, items: draft.changes.filter((c) => c.effect === e) })).filter((g) => g.items.length)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="gap-0 p-0 sm:max-w-xl">
        <DialogHeader className="border-b px-5 py-4">
          <DialogTitle className="flex items-center gap-2">
            Review changes
            <span className="font-mono text-xs font-normal text-muted-foreground">v{draft.base_version} → v{agent.version + 1}</span>
          </DialogTitle>
        </DialogHeader>
        <div className="max-h-[60vh] overflow-y-auto">
          {draft.stale && (
            <div className="flex items-center gap-2 border-b bg-warning/5 px-5 py-2.5 text-sm text-warning">
              <TriangleAlert className="size-4" />Changed to v{agent.version} since your draft began
            </div>
          )}
          {groups.map((g) => (
            <section key={g.e} className="border-b px-5 py-3 last:border-b-0">
              <div className="mb-1.5 flex items-center gap-2">
                <EffectTag effect={g.e} />
                <span className="text-xs text-muted-foreground">{EFFECT[g.e].fact}</span>
              </div>
              <ChangeList changes={g.items} showEffect={false} />
            </section>
          ))}
        </div>
        <DialogFooter className="m-0 items-center px-5 py-3 sm:justify-between">
          <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note" className="h-8 sm:max-w-64" />
          <div className="flex gap-2">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button disabled={apply.isPending || draft.stale} onClick={() => apply.mutate(note, { onSuccess: () => { setNote(""); onOpenChange(false) } })}>
              Apply as v{agent.version + 1}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

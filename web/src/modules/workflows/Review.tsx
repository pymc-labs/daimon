import { ArrowRight, KeyRound } from "lucide-react"
import { useMemo, useState } from "react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"
import { useCatalog, usePublish, useRequestPublish } from "./api"
import { EFFECT, GRANT, KIND, TRIGGER } from "./kinds"
import { reviewCount, reviewDiff, type FieldChange, type Line, type Review } from "./review"
import { titleOf } from "./spec"
import type { Draft, Problem, Workflow } from "./types"


/** Instruction lines with their +/- marks, so the diff reads without color. */
export function LineDiff({ lines }: { lines: Line[] }) {
  return (
    <div className="overflow-hidden rounded-md border bg-muted/30 font-mono text-xs leading-5">
      {lines.map((l, i) => (
        <div key={i} className={cn("flex gap-2 px-2", l.op === "add" && "bg-success/10", l.op === "del" && "bg-destructive/10")}>
          <span aria-hidden className="w-2 shrink-0 text-muted-foreground select-none">{l.op === "add" ? "+" : l.op === "del" ? "-" : ""}</span>
          <span className="sr-only">{l.op === "add" ? "Added: " : l.op === "del" ? "Removed: " : ""}</span>
          <span className={cn("min-w-0 break-words whitespace-pre-wrap", l.op === "del" && "text-muted-foreground line-through decoration-destructive/40", l.op === "keep" && "text-muted-foreground")}>{l.text || " "}</span>
        </div>
      ))}
    </div>
  )
}

/** Label, then what it was and what it is now. */
export function ChangeRows({ rows }: { rows: FieldChange[] }) {
  return (
    <dl className="space-y-1 text-sm">
      {rows.map((c) => (
        <div key={c.label} className="grid grid-cols-[168px_1fr] gap-3 max-sm:grid-cols-1 max-sm:gap-0.5">
          <dt className="truncate text-muted-foreground" title={c.label}>{c.label}</dt>
          <dd className="flex min-w-0 flex-wrap items-center gap-x-1.5 break-words">
            {c.from != null && <span className={cn(c.to != null ? "text-muted-foreground" : "")}>{c.to != null ? c.from : <><span className="line-through decoration-destructive/50">{c.from}</span><span className="sr-only"> removed</span></>}</span>}
            {c.from != null && c.to != null && <ArrowRight className="size-3 shrink-0 text-faint" aria-label="to" />}
            {c.to != null && <span className="font-medium">{c.to}{c.from == null && <span className="sr-only"> added</span>}</span>}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function Heading({ children, n }: { children: React.ReactNode; n?: number }) {
  return <h3 className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">{children}{n != null && <span className="tabular">{n}</span>}</h3>
}

/** Steps, triggers and settings that differ, then "No changes" when none do. */
export function ReviewSections({ r }: { r: Review }) {
  const settings = [...r.workflow, ...r.access]
  return (
    <>
      {r.nodes.length > 0 && (
        <section>
          <Heading n={r.nodes.length}>Steps</Heading>
          <div className="space-y-3">
            {r.nodes.map((n) => {
              const E = EFFECT[n.effect], K = KIND[n.kind]
              return (
                <div key={n.id} className="space-y-2">
                  <div className="flex h-7 items-center gap-2 text-sm">
                    <K.icon className={cn("size-3.5", K.text)} /><span className="font-medium">{n.title}</span>
                    <span className={cn("ml-auto flex items-center gap-1 text-xs", E.cls)}><E.icon className="size-3.5" />{E.label}</span>
                  </div>
                  {n.instructions && <LineDiff lines={n.instructions} />}
                  {n.changes.length > 0 && <ChangeRows rows={n.changes} />}
                </div>
              )
            })}
          </div>
        </section>
      )}
      {r.triggers.length > 0 && (
        <section>
          <Heading n={r.triggers.length}>Triggers</Heading>
          <div className="space-y-3">
            {r.triggers.map((t) => {
              const K = TRIGGER[t.kind], E = EFFECT[t.effect]
              return (
                <div key={t.id} className="space-y-2">
                  <div className="flex h-7 items-center gap-2 text-sm"><K.icon className="size-3.5 text-muted-foreground" /><span className="font-medium">{K.label}</span>
                    <span className={cn("ml-auto flex items-center gap-1 text-xs", E.cls)}><E.icon className="size-3.5" />{E.label}</span></div>
                  <ChangeRows rows={t.changes} />
                </div>
              )
            })}
          </div>
        </section>
      )}
      {settings.length > 0 && <section><Heading>Access and settings</Heading><ChangeRows rows={settings} /></section>}
      {reviewCount(r) === 0 && <p className="text-sm text-muted-foreground">No changes</p>}
    </>
  )
}

/** What a draft changes against the live version, in the doc's order: note, nodes, triggers, access, grants, runs as. */
export function ReviewBody({ wf, draft, note, setNote }: { wf: Workflow; draft: Draft; note: string; setNote: (s: string) => void }) {
  const { data: catalog } = useCatalog()
  const live = wf.live_version ? wf.spec : null
  const r = useMemo(() => reviewDiff(live, draft.spec, { catalog }), [live, draft.spec, catalog])
  const grants = draft.new_grants
  const errors = draft.validation.errors
  const next = (wf.live_version ?? 0) + 1
  return (
    <div className="max-h-[62vh] space-y-5 overflow-y-auto px-5 py-4">
      <div className="space-y-1.5">
        <Label htmlFor="note" className="text-xs font-medium text-muted-foreground">Version note</Label>
        <Input id="note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed and why" className="h-8" />
      </div>
      <ReviewSections r={r} />
      {grants.length > 0 && (
        <section>
          <Heading n={grants.length}>Grants needing an admin</Heading>
          <ul>
            {grants.map((g, i) => (
              <li key={i}>
                <div className="flex h-8 items-center gap-2 text-sm">
                  <KeyRound className="size-3.5 shrink-0 text-warning" /><span className="font-medium">{GRANT[g.kind]}</span>
                  <span className="ml-auto truncate text-xs text-muted-foreground">{g.node ? titleOf(g.node, draft.spec.nodes[g.node]) : g.detail}</span>
                </div>
                {g.kind === "scoped_write" && <p className="pb-1.5 pl-5.5 text-xs text-muted-foreground">{g.detail}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
      <section>
        <Heading>Runs as</Heading>
        <p className="text-sm">You, from v{next}</p>
      </section>
      {errors.length > 0 && (
        <section>
          <Heading n={errors.length}>Problems</Heading>
          <ul className="space-y-1 text-sm">{errors.map((p: Problem, i) => <li key={i}>{p.message}</li>)}</ul>
        </section>
      )}
    </div>
  )
}

/** Publish, or Request approval when the draft adds a grant and you are not an admin. */
export function ReviewDialog({ wf, draft, open, onOpenChange }: { wf: Workflow; draft: Draft; open: boolean; onOpenChange: (o: boolean) => void }) {
  const [note, setNote] = useState("")
  const publish = usePublish(wf.id)
  const request = useRequestPublish(wf.id)
  const needs = draft.new_grants.length > 0 && !wf.is_admin
  const errors = draft.validation.errors
  const act = needs ? request : publish
  const done = { onSuccess: () => onOpenChange(false) }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="gap-0 p-0 sm:max-w-2xl">
        <DialogHeader className="border-b px-5 py-4"><DialogTitle>Publish v{(wf.live_version ?? 0) + 1}</DialogTitle></DialogHeader>
        <ReviewBody wf={wf} draft={draft} note={note} setNote={setNote} />
        <DialogFooter className="m-0 flex-col items-stretch gap-2 border-t px-5 py-3 sm:flex-col sm:justify-start">
          <div className="flex justify-end gap-2 max-sm:[&>*]:flex-1">
            <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button disabled={errors.length > 0 || act.isPending} onClick={() => act.mutate(note, done)}>{needs ? "Request approval" : "Publish"}</Button>
          </div>
          {errors.length > 0 && <p className="text-right text-sm text-destructive">{errors.length === 1 ? "Fix 1 problem to publish" : `Fix ${errors.length} problems to publish`}</p>}
          {act.error && <p className="text-right text-sm text-destructive">{act.error.message}</p>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

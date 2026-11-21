import { Check, Eye } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Dot } from "./ui"
import { Button } from "@/components/ui/button"
import { useConfirm } from "@/lib/confirm"
import { cn } from "@/lib/utils"
import { discardDraft, useDraft } from "./api"
import { ReviewDialog } from "./Review"
import { choice } from "./run/lib"
import type { Workflow } from "./types"
import { useQueryClient } from "@tanstack/react-query"

/** A titled block of a tab, divided from the one above. */
export function Part({ title, aside, children, className }: { title?: string; aside?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("border-t py-5 first:border-t-0 first:pt-0", className)}>
      {(title || aside) && (
        <div className="mb-3 flex min-h-8 items-center gap-2">
          {title && <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>}
          {aside && <div className="ml-auto flex items-center gap-2">{aside}</div>}
        </div>
      )}
      {children}
    </section>
  )
}

export type Option<T extends string> = { value: T; label: string; fact?: string; icon?: React.ReactNode }

/** Single choice as large cards; the whole card is the target, a check marks the pick. */
export function ChoiceCards<T extends string>({ value, onChange, options, label, cols = 2, disabled }: {
  value: T | undefined; onChange: (v: T) => void; options: Option<T>[]; label: string; cols?: 2 | 3 | 4; disabled?: boolean
}) {
  return (
    <div role="radiogroup" aria-label={label} className={cn("grid gap-2", cols === 2 && "sm:grid-cols-2", cols === 3 && "sm:grid-cols-3", cols === 4 && "grid-cols-2 sm:grid-cols-4")}>
      {options.map((o) => {
        const on = value === o.value
        return (
          <button key={o.value} type="button" role="radio" aria-checked={on} disabled={disabled} onClick={() => onChange(o.value)} className={cn(choice(on), "relative pr-10 disabled:opacity-60")}>
            <span className="flex items-center gap-2 text-sm font-medium">{o.icon}{o.label}</span>
            {o.fact && <span className="text-xs text-muted-foreground">{o.fact}</span>}
            {on && <Check className="absolute right-3.5 top-3 size-4 text-link" />}
          </button>
        )
      })}
    </div>
  )
}

/** Unpublished edits made on a tab: count, Discard and Review. Hidden when the draft matches the live version. */
export function EditStrip({ wf }: { wf: Workflow }) {
  const { data: draft } = useDraft(wf.id, wf.can_edit)
  const qc = useQueryClient()
  const confirm = useConfirm()
  const [review, setReview] = useState(false)
  const n = draft?.changes.length ?? 0
  if (!draft || !n || !wf.can_edit) return null
  const discard = async () => {
    if (!(await confirm({ title: "Discard draft?", action: "Discard", destructive: true }))) return
    qc.setQueryData(["workflows", wf.id, "draft"], await discardDraft(wf.id))
    void qc.invalidateQueries({ queryKey: ["workflows"] })
    toast("Draft discarded")
  }
  return (
    <>
      <div role="status" className="flex min-h-11 shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b bg-popover px-4 py-2 text-sm lg:px-6">
        <span className="flex items-center gap-2 font-medium"><Dot /><span className="tabular">{n} {n === 1 ? "change" : "changes"}</span></span>
        <span className="text-muted-foreground">not published</span>
        <div className="ml-auto flex gap-2 max-sm:w-full max-sm:[&>*]:flex-1">
          <Button variant="ghost" size="sm" onClick={() => void discard()}>Discard</Button>
          <Button size="sm" onClick={() => setReview(true)}><Eye />Review</Button>
        </div>
      </div>
      <ReviewDialog wf={wf} draft={draft} open={review} onOpenChange={setReview} />
    </>
  )
}

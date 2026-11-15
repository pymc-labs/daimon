import { TriangleAlert } from "lucide-react"

import { cn } from "@/lib/utils"
import type { ModelPreview } from "./types"

/** The switch card's lines (what stops working, any shared-thread limit), or why the pick can't go ahead. */
export function ModelPreviewPanel({ preview, className }: { preview: ModelPreview; className?: string }) {
  if (preview.refusal) {
    return (
      <div className={cn("flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-sm text-destructive", className)}>
        <TriangleAlert className="mt-0.5 size-4 shrink-0" />{preview.refusal.line}
      </div>
    )
  }
  if (!preview.card || !preview.lines.length) return null
  return (
    <ul className={cn("space-y-1 rounded-lg border bg-muted/40 px-3 py-2.5 text-sm", className)}>
      {preview.lines.map((line) => <li key={line}>{line}</li>)}
    </ul>
  )
}

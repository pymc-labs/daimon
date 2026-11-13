import { ChevronDown, ChevronUp, Link2, X } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { PANEL } from "../Canvas"

/** Non-modal step detail docked over the right of a run: the graph stays visible and live. */
export function StepPanel({ title, index, count, step, close, children }: {
  title: string; index: number; count: number; step: (d: number) => void; close: () => void; children: React.ReactNode
}) {
  return (
    <aside aria-label="Step detail" className={PANEL}
      onKeyDown={(e) => {
        if ((e.target as HTMLElement).closest("input, textarea, [contenteditable]")) return
        if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); step(1) }
        if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); step(-1) }
        if (e.key === "Escape") close()
      }}>
      <div className="flex h-11 shrink-0 items-center gap-1 border-b px-3">
        <span className="mr-auto pl-1 text-xs font-medium text-muted-foreground">{title}</span>
        {count > 0 && (
          <>
            <span className="mr-1 text-xs text-faint tabular">{index + 1} / {count}</span>
            <Button variant="ghost" size="icon-sm" aria-label="Previous" disabled={index <= 0} onClick={() => step(-1)}><ChevronUp /></Button>
            <Button variant="ghost" size="icon-sm" aria-label="Next" disabled={index >= count - 1} onClick={() => step(1)}><ChevronDown /></Button>
          </>
        )}
        <Button variant="ghost" size="icon-sm" aria-label="Copy link" onClick={() => { void navigator.clipboard.writeText(location.href); toast("Link copied") }}><Link2 /></Button>
        <Button variant="ghost" size="icon-sm" aria-label="Close" onClick={close}><X /></Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </aside>
  )
}

import { TriangleAlert } from "lucide-react"

import { cn } from "@/lib/utils"
import type { ModelPreview } from "./types"

const DROP_LABEL: Record<string, string> = { memory: "Memory", sandbox: "Sandbox" }

const dropLabel = (d: string) => DROP_LABEL[d] ?? d

/** What a model pick drops, limits and costs, and what happens to threads. */
export function ModelPreviewPanel({ preview, className }: { preview: ModelPreview; className?: string }) {
  if (preview.refusal) {
    return (
      <div className={cn("flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-sm text-destructive", className)}>
        <TriangleAlert className="mt-0.5 size-4 shrink-0" />{preview.refusal.line}
      </div>
    )
  }
  if (!preview.moves) return null
  const lost = [...new Set([...preview.drops.map(dropLabel), ...preview.skills, ...preview.servers, ...preview.capabilities])]
  const rows: [string, string[]][] = [
    ["Stops working", lost],
    ["Shared threads", preview.shared_limits],
    ["Cost", preview.cost ? [preview.cost] : []],
    ["Threads", preview.threads ? [preview.threads] : []],
  ]
  return (
    <div className={cn("space-y-2.5 rounded-lg border bg-muted/40 px-3 py-3 text-sm", className)}>
      {rows.filter(([, v]) => v.length).map(([label, items]) => (
        <div key={label} className="grid grid-cols-[104px_1fr] gap-3">
          <span className="text-muted-foreground">{label}</span>
          <ul className="space-y-0.5">{items.map((i) => <li key={i}>{i}</li>)}</ul>
        </div>
      ))}
    </div>
  )
}

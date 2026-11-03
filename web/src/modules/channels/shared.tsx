import { Globe } from "lucide-react"

import { BrandIcon } from "@/components/brand"
import { cn } from "@/lib/utils"
import type { Outcome } from "./api"

/** What the last save said: its notes as plain labels, or why it was refused. */
export function Notice({ out }: { out: Outcome | null }) {
  if (!out || (!out.error && !out.notes.length)) return null
  return (
    <div className="mt-2 space-y-0.5 text-xs" role={out.error ? "alert" : "status"}>
      {out.error && <div className="text-destructive">{out.error}</div>}
      {out.notes.map((n) => <div key={n} className="text-muted-foreground">{n}</div>)}
    </div>
  )
}

export const PlatformIcon = ({ platform, className }: { platform: string; className?: string }) =>
  platform === "web" ? <Globe className={className ?? "size-4 shrink-0 text-muted-foreground"} /> : <BrandIcon brand={platform} className={className} />

/** A label and its value or control; the control drops under the label on narrow screens. */
export function Line({ label, children, top }: { label: string; children: React.ReactNode; top?: boolean }) {
  return (
    <div className={cn("grid grid-cols-[112px_1fr] gap-3 max-sm:grid-cols-1 max-sm:gap-1", top ? "items-start" : "items-center")}>
      <span className={cn("text-muted-foreground", top && "pt-1")}>{label}</span>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

/** A short list of rows under a section; empty lists render nothing. */
export const Rows = ({ children }: { children: React.ReactNode }) => <ul className="divide-y rounded-md border text-sm">{children}</ul>

import { Globe } from "lucide-react"

import { BrandIcon } from "@/components/brand"
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

/** A short list of rows under a section; empty lists render nothing. */
export const Rows = ({ children }: { children: React.ReactNode }) => <ul className="divide-y rounded-md border text-sm">{children}</ul>

import { CircleCheck, Hourglass } from "lucide-react"
import { Link } from "react-router"

import { Ago } from "@/components/common"
import { Button } from "@/components/ui/button"
import type { Workflow } from "./types"

/** The publish state across the page: a request waiting for an admin, or the version just published. */
export function PublishStrip({ wf }: { wf: Workflow }) {
  const r = wf.pending_request
  const p = wf.published
  if (r) {
    return (
      <div role="status" className="flex min-h-11 shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b bg-popover px-4 py-2 text-sm lg:px-6">
        <span className="flex items-center gap-1.5 font-medium"><Hourglass className="size-4 text-warning" />Waiting for admin approval</span>
        <span className="min-w-0 truncate text-muted-foreground">v{r.version} requested by {r.by.name}, <Ago iso={r.at} />{r.grants.length > 0 && `, ${r.grants.length} new ${r.grants.length === 1 ? "grant" : "grants"}`}</span>
        {wf.is_admin && <Button asChild variant="outline" size="sm" className="ml-auto max-sm:w-full"><Link to="/inbox">Open in inbox</Link></Button>}
      </div>
    )
  }
  if (!p || wf.draft_changes > 0) return null
  return (
    <div role="status" className="flex min-h-11 shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b bg-popover px-4 py-2 text-sm lg:px-6">
      <span className="flex items-center gap-1.5 font-medium"><CircleCheck className="size-4 text-success" />Published v{p.version}</span>
      <span className="min-w-0 truncate text-muted-foreground">
        by {p.by.name}, <Ago iso={p.at} />{p.approved_by && `, approved by ${p.approved_by.name}`}{p.note && `, ${p.note}`}
      </span>
    </div>
  )
}

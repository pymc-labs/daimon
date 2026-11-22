import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { Ago, StatusDot } from "@/components/common"
import { Empty } from "@/components/filters"
import { PanelSection, Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { api } from "@/lib/api"
import { exact, plural } from "@/lib/format"
import { KEY } from "./bits"
import type { ArtifactDetail, ArtifactEvent } from "./types"

const preview = (d: unknown) => (typeof d === "string" ? d : JSON.stringify(d)).slice(0, 90)
const SENDER: Record<ArtifactEvent["sender"], string> = { member: "Member", visitor: "Visitor", anonymous: "Anonymous" }

export function Activity({ a }: { a: ArtifactDetail }) {
  const q = useQuery({ queryKey: [...KEY, "events", a.id], queryFn: () => api.get<ArtifactEvent[]>(`/api/artifacts/${a.id}/events`), enabled: a.events })
  const events = q.data ?? []
  const none = !a.events && !a.watches.length && !a.ask
  if (none) return <Empty>No activity</Empty>
  return (
    <>
      {a.events && (
        <PanelSection title="Events" className="border-t-0">
          <QueryState query={q} empty="No events yet" className="py-6">
            {() => (
              <ul className="-mx-1 space-y-0.5">
                {events.map((e) => (
                  <li key={e.id} className="rounded-md px-1 py-1.5">
                    <div className="flex items-center gap-2 text-sm">
                      <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] font-medium">{e.event}</span>
                      <span className="shrink-0 text-xs text-muted-foreground"><Ago iso={e.at} /></span>
                    </div>
                    <div className="mt-0.5 truncate font-mono text-xs text-faint">{preview(e.data)}</div>
                    <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
                      <Tag>{SENDER[e.sender]}</Tag>
                      <span className="truncate">{e.by ?? "Anonymous"}</span>
                      <span className="ml-auto shrink-0 tabular">{plural(e.delivered, "delivery", "deliveries")}</span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
        </PanelSection>
      )}
      {a.watches.length > 0 && (
        <PanelSection title="Watches" className={a.events ? "" : "border-t-0"}>
          <ul className="space-y-3">
            {a.watches.map((w) => (
              <li key={w.id} className="text-sm">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-medium">{w.name}</span>
                  <StatusDot status={w.status === "active" ? "healthy" : "setup"} label={w.status === "active" ? "Active" : "Paused"} />
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="truncate">{w.owner}, {w.agent}</span>
                  <span className="ml-auto shrink-0 tabular">{exact(w.turns_today)} of {exact(w.max_turns_per_day)} turns today</span>
                </div>
                {w.thread_url && <Link to={w.thread_url} className="mt-1 inline-block text-xs text-link hover:underline">Open thread</Link>}
              </li>
            ))}
          </ul>
        </PanelSection>
      )}
      {a.ask && (
        <PanelSection title="Asks" className={a.events || a.watches.length ? "" : "border-t-0"}>
          <div className="flex gap-6 text-sm">
            <div><div className="font-medium tabular">{exact(a.asks.threads)}</div><div className="text-xs text-muted-foreground">Threads</div></div>
            <div><div className="font-medium tabular">{exact(a.asks.turns_7d)}</div><div className="text-xs text-muted-foreground">Questions in 7 days</div></div>
          </div>
        </PanelSection>
      )}
    </>
  )
}

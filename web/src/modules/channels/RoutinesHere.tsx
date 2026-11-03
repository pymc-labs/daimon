import { ChevronRight } from "lucide-react"
import { Link } from "react-router"

import { useMe } from "@/lib/hooks"
import { PanelSection, Tag } from "@/modules/admin/shared"
import { cronWords, RELATIONS } from "./lib"
import { Rows } from "./shared"
import type { ChannelSettings } from "./types"

export function RoutinesHere({ s }: { s: ChannelSettings }) {
  const { data: me } = useMe()
  if (!s.routines.length) return null
  const linked = !!me?.modules.includes("routines")
  return (
    <PanelSection title="Routines here">
      <Rows>
        {s.routines.map((r) => {
          const body = (
            <>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{r.agent}</span>
                <span className="block truncate text-xs text-muted-foreground">{cronWords(r.cron)}</span>
              </span>
              <span className="flex shrink-0 flex-wrap justify-end gap-1">
                {!r.enabled && <Tag>Paused</Tag>}
                <Tag>{RELATIONS[r.relation]}</Tag>
              </span>
              {linked && <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}
            </>
          )
          const cls = "flex items-center gap-3 px-3 py-2"
          return (
            <li key={r.id}>
              {linked ? <Link to={`/routines?routine=${encodeURIComponent(r.id)}`} className={`${cls} hover:bg-accent/50`}>{body}</Link> : <div className={cls}>{body}</div>}
            </li>
          )
        })}
      </Rows>
    </PanelSection>
  )
}

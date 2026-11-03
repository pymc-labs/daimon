import { Ago } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { PanelSection } from "@/modules/admin/shared"
import { useChannelHistory } from "./api"
import { describeChange } from "./lib"
import type { ChannelDetail } from "./types"

const SURFACE = { web: "Web", chat: "Chat", panel: "Panel", cli: "CLI" } as const

export function History({ c }: { c: ChannelDetail }) {
  const q = useChannelHistory(c.id, c.can_manage)
  return (
    <PanelSection title="History">
      <QueryState query={q} empty="No changes yet">{(rows) => (
        <ul className="space-y-2.5 text-sm">
          {rows.map((e, i) => {
            const d = describeChange(e, { readers: c.readers_options, writers: c.writers_options })
            return (
              <li key={`${e.at}${i}`}>
                <div className="flex items-baseline justify-between gap-3">
                  <span className="font-medium">{d.what}</span>
                  <span className="shrink-0 text-xs text-muted-foreground"><Ago iso={e.at} /></span>
                </div>
                <div className="break-words text-muted-foreground">
                  {[d.from, d.to].filter(Boolean).join(" to ")}
                </div>
                <div className="text-xs text-faint">{[e.actor?.name, e.surface && SURFACE[e.surface]].filter(Boolean).join(", ")}</div>
              </li>
            )
          })}
        </ul>
      )}</QueryState>
    </PanelSection>
  )
}

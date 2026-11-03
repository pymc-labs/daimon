import { AgentLink, RoutineLink } from "@/components/links"
import { PanelSection, Tag } from "@/components/panel"
import { agentOf, cronWords, RELATIONS } from "./lib"
import { Rows } from "./shared"
import type { ChannelSettings } from "./types"

export function RoutinesHere({ s }: { s: ChannelSettings }) {
  if (!s.routines.length) return null
  return (
    <PanelSection title="Routines">
      <Rows>
        {s.routines.map((r) => {
          const who = agentOf(s.agents, r.agent)!
          return (
            <li key={r.id} className="flex items-center gap-3 px-3 py-2">
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium"><RoutineLink id={r.id} name={r.title} /></span>
                <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
                  <AgentLink id={who.id} name={who.name} avatar={who.avatar} /><span>·</span><span className="truncate">{cronWords(r.cron)}</span>
                </span>
              </span>
              <span className="flex shrink-0 flex-wrap justify-end gap-1">
                {!r.enabled && <Tag>Paused</Tag>}
                <Tag>{RELATIONS[r.relation]}</Tag>
              </span>
            </li>
          )
        })}
      </Rows>
    </PanelSection>
  )
}

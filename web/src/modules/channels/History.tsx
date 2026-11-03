import { ArrowRight } from "lucide-react"

import { Ago } from "@/components/common"
import { AgentLink, PersonLink } from "@/components/links"
import { Empty } from "@/components/filters"
import { QueryState } from "@/components/QueryState"
import { PanelSection } from "@/components/panel"
import { useChannelHistory } from "./api"
import { agentOf, describeChange, type Part } from "./lib"
import type { AgentRefs, ChannelDetail } from "./types"

const SURFACE = { web: "Web", chat: "Chat", panel: "Panel", cli: "CLI" } as const

function Parts({ parts, refs }: { parts: Part[]; refs: AgentRefs }) {
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-x-0.5">
      {parts.map((p, i) => {
        if (typeof p === "string") return <span key={i} className="whitespace-pre-wrap">{p}</span>
        const a = agentOf(refs, p.agent)!
        return <AgentLink key={i} id={a.id} name={a.name} avatar={a.avatar} />
      })}
    </span>
  )
}

/** The changes made to a channel's settings, newest first. */
export function History({ c }: { c: ChannelDetail }) {
  const q = useChannelHistory(c.id, c.can_manage)
  return (
    <PanelSection>
      <QueryState query={q}>{({ entries, agents }) => !entries.length ? <Empty>No changes yet</Empty> : (
        <ul className="space-y-3 text-sm">
          {entries.map((e, i) => {
            const d = describeChange(e, { readers: c.readers_options, writers: c.writers_options })
            const who = d.agent ? agentOf(agents, d.agent)! : null
            return (
              <li key={`${e.at}${i}`}>
                <div className="flex items-baseline justify-between gap-3">
                  <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="font-medium">{d.what}</span>
                    {who && <span className="text-xs text-muted-foreground"><AgentLink id={who.id} name={who.name} avatar={who.avatar} /></span>}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground"><Ago iso={e.at} /></span>
                </div>
                <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-muted-foreground">
                  {d.from.length > 0 && <Parts parts={d.from} refs={agents} />}
                  {d.from.length > 0 && d.to.length > 0 && <ArrowRight className="size-3.5 shrink-0" aria-label="to" />}
                  {d.to.length > 0 && <Parts parts={d.to} refs={agents} />}
                </div>
                <div className="flex items-center gap-3 text-xs text-faint">
                  {e.actor && <PersonLink id={e.actor.id} name={e.actor.name} />}
                  {e.surface && <span>{SURFACE[e.surface]}</span>}
                </div>
              </li>
            )
          })}
        </ul>
      )}</QueryState>
    </PanelSection>
  )
}

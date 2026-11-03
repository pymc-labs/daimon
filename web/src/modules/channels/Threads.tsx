import { useState } from "react"

import { AgentLink, ChannelLink } from "@/components/links"
import { PanelSection } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { useChannels, useSave, type Outcome } from "./api"
import { agentOf, THREAD_KINDS, threadValue } from "./lib"
import { Notice, Rows } from "./shared"
import type { ChannelDetail, ChannelSettings } from "./types"

export function Threads({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const { data: list } = useChannels()
  const [out, setOut] = useState<Outcome | null>(null)
  if (!s.threads.length) return null
  const labels = { readers: c.readers_options, writers: c.writers_options }
  const listed = new Set((list?.channels ?? []).map((x) => x.id))

  return (
    <PanelSection title="Threads with their own settings">
      <Rows>
        {s.threads.map((t) => {
          const name = t.name ?? "Unnamed thread"
          const who = t.kind === "handoff" ? agentOf(s.agents, t.value) : null
          return (
            <li key={`${t.thread_id}/${t.kind}`} className="flex items-center gap-3 px-3 py-2">
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium"><ChannelLink id={listed.has(t.thread_id) ? t.thread_id : null} name={name} kind="thread" /></span>
                <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
                  <span className="shrink-0">{THREAD_KINDS[t.kind]}:</span>
                  {who ? <AgentLink id={who.id} name={who.name} avatar={who.avatar} /> : <span className="truncate">{threadValue(t.kind, t.value, labels)}</span>}
                </span>
              </span>
              {c.can_manage && (
                <Button size="xs" variant="ghost" disabled={save.pending} aria-label={`Clear ${THREAD_KINDS[t.kind]} in ${name}`}
                  onClick={async () => setOut(await save.run("post", "threads/clear", { thread_id: t.thread_id, kind: t.kind }))}>Clear</Button>
              )}
            </li>
          )
        })}
      </Rows>
      <Notice out={out} />
    </PanelSection>
  )
}

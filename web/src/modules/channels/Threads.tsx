import { useState } from "react"

import { Button } from "@/components/ui/button"
import { PanelSection } from "@/modules/admin/shared"
import { useSave, type Outcome } from "./api"
import { THREAD_KINDS, threadValue } from "./lib"
import { Notice, Rows } from "./shared"
import type { ChannelDetail, ChannelSettings } from "./types"

export function Threads({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const [out, setOut] = useState<Outcome | null>(null)
  if (!s.threads.length) return null
  const labels = { readers: c.readers_options, writers: c.writers_options }

  return (
    <PanelSection title="Threads with their own settings">
      <Rows>
        {s.threads.map((t) => (
          <li key={`${t.thread_id}/${t.kind}`} className="flex items-center gap-3 px-3 py-2">
            <span className="min-w-0 flex-1">
              <span className="block truncate font-medium">{THREAD_KINDS[t.kind]}</span>
              <span className="block truncate text-xs text-muted-foreground">{threadValue(t.kind, t.value, labels)}</span>
            </span>
            <span className="max-w-24 truncate font-mono text-xs text-faint" title={t.thread_id}>{t.thread_id}</span>
            {c.can_manage && (
              <Button size="xs" variant="outline" disabled={save.pending} aria-label={`Clear ${THREAD_KINDS[t.kind]} in ${t.thread_id}`}
                onClick={async () => setOut(await save.run("post", "threads/clear", { thread_id: t.thread_id, kind: t.kind }))}>Clear</Button>
            )}
          </li>
        ))}
      </Rows>
      <Notice out={out} />
    </PanelSection>
  )
}

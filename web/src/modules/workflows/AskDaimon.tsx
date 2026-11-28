import { Sparkles, Undo2, X } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Thread } from "@/modules/chat/Thread"
import type { SendOpts } from "@/modules/chat/turns"
import type { AgentRef } from "@/lib/types"
import { cn } from "@/lib/utils"
import { PANEL } from "./Canvas"
import { KIND } from "./kinds"
import type { TurnEdit } from "./live"
import { titleOf } from "./spec"
import type { Spec } from "./types"

const SUGGEST = ["Add an approval before send", "Post the result to #sales", "Retry the send twice", "Cap each run at $1"]

/** Chat with Daimon about the draft, in the workflow's own thread; each reply may edit the draft. */
export function AskDaimon({ spec, sid, agent, edits, send, select, close }: {
  spec: Spec; sid?: string; agent?: AgentRef; edits: Record<string, TurnEdit>; send: SendOpts; select: (id: string) => void; close: () => void
}) {
  const [fill, setFill] = useState<{ text: string; id: number }>()
  return (
    <aside aria-label="Ask Daimon" className={PANEL}>
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        <span className="flex size-6 items-center justify-center rounded-md border bg-background text-link"><Sparkles className="size-3.5" /></span>
        <span className="text-sm font-medium">Ask Daimon</span>
        <Button variant="ghost" size="icon-sm" className="ml-auto" onClick={close} aria-label="Close"><X /></Button>
      </div>
      {sid && agent && (
        <Thread
          id={sid} agent={agent} compact placeholder="Change the workflow" send={send} fill={fill}
          empty={<div className="w-fit rounded-xl border bg-background px-3 py-2 text-sm">What should change?</div>}
          after={(m) => {
            const e = edits[m.id]
            const nodes = e?.nodes.filter((id) => spec.nodes[id])
            if (!e || !(nodes?.length || e.undo)) return null
            return (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {nodes?.map((id) => {
                  const K = KIND[spec.nodes[id]!.kind]
                  return (
                    <button key={id} onClick={() => select(id)} className="flex h-6 items-center gap-1.5 rounded-md border bg-background px-2 text-xs hover:bg-accent">
                      <K.icon className={cn("size-3", K.text)} />{titleOf(id, spec.nodes[id])}
                    </button>
                  )
                })}
                {e.undo && (
                  <button onClick={e.undo} className="flex h-6 items-center gap-1.5 rounded-md px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"><Undo2 className="size-3" />Undo</button>
                )}
              </div>
            )
          }}
          above={(
            <div className="flex flex-wrap gap-1.5">
              {SUGGEST.map((s) => <button key={s} onClick={() => setFill({ text: s, id: Date.now() })} className="h-7 rounded-md border px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground">{s}</button>)}
            </div>
          )}
        />
      )}
    </aside>
  )
}

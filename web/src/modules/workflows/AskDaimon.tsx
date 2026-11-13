import { ArrowUp, Sparkles, Undo2, X } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { PANEL } from "./Canvas"
import { titleOf } from "./spec"
import type { AskMessage, Spec } from "./types"
import { KIND } from "./kinds"

const SUGGEST = ["Add an approval before send", "Post the result to #sales", "Retry the send twice", "Cap each run at $1"]
export type AskTurn = AskMessage & { changed?: string[]; undo?: () => void }

/** Chat with Daimon about the draft; each reply edits the draft. */
export function AskDaimon({ spec, messages, pending, send, select, close }: {
  spec: Spec; messages: AskTurn[]; pending: boolean; send: (text: string) => void; select: (id: string) => void; close: () => void
}) {
  const [text, setText] = useState("")
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }) }, [messages.length, pending])
  const go = (t: string) => { if (!t.trim() || pending) return; send(t.trim()); setText("") }
  return (
    <aside aria-label="Ask Daimon" className={PANEL}>
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        <span className="flex size-6 items-center justify-center rounded-md border bg-background text-link"><Sparkles className="size-3.5" /></span>
        <span className="text-sm font-medium">Ask Daimon</span>
        <Button variant="ghost" size="icon-sm" className="ml-auto" onClick={close} aria-label="Close"><X /></Button>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {messages.map((m, i) => (
          <div key={i} className={cn("flex flex-col gap-1.5", m.role === "user" && "items-end")}>
            <div className={cn("max-w-[88%] rounded-xl px-3 py-2 text-sm whitespace-pre-wrap", m.role === "user" ? "bg-link text-primary-foreground" : "border bg-background")}>{m.text}</div>
            {!!m.changed?.length && (
              <div className="flex flex-wrap gap-1.5">
                {m.changed.filter((id) => spec.nodes[id]).map((id) => {
                  const K = KIND[spec.nodes[id]!.kind]
                  return (
                    <button key={id} onClick={() => select(id)} className="flex h-6 items-center gap-1.5 rounded-md border bg-background px-2 text-xs hover:bg-accent">
                      <K.icon className={cn("size-3", K.text)} />{titleOf(id, spec.nodes[id])}
                    </button>
                  )
                })}
                {m.undo && i === messages.length - 1 && (
                  <button onClick={m.undo} className="flex h-6 items-center gap-1.5 rounded-md px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"><Undo2 className="size-3" />Undo</button>
                )}
              </div>
            )}
          </div>
        ))}
        {pending && <div className="w-fit rounded-xl border bg-background px-3 py-2 text-sm text-muted-foreground">Editing the draft</div>}
        <div ref={end} />
      </div>
      <div className="shrink-0 space-y-2 border-t p-3">
        <div className="flex flex-wrap gap-1.5">
          {SUGGEST.map((s) => <button key={s} disabled={pending} onClick={() => go(s)} className="h-7 rounded-md border px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50">{s}</button>)}
        </div>
        <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); go(text) }}>
          <Textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="Change the workflow" rows={3} className="resize-none text-sm"
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(text) } }} />
          <div className="flex justify-end"><Button type="submit" size="sm" disabled={!text.trim() || pending} className="max-sm:flex-1"><ArrowUp />Send</Button></div>
        </form>
      </div>
    </aside>
  )
}

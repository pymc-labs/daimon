import { useQueryClient } from "@tanstack/react-query"
import { Loader2 } from "lucide-react"
import { useEffect, useLayoutEffect, useRef, useState } from "react"

import { ErrorState } from "@/components/QueryState"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { useCan } from "@/lib/hooks"
import type { AgentRef } from "@/lib/types"
import { cn } from "@/lib/utils"
import { MonitoringNotice } from "./Cards"
import { Composer } from "./Composer"
import { AssistantMessage, UserMessage } from "./Message"
import type { Msg, Notice } from "./types"
import { useCredit, useSessionDetail } from "./threadHooks"
import { answer, hydrate, loadEarlier, resume, send, stop, switchBack, useThread, type SendOpts } from "./turns"

export type ThreadProps = {
  id: string
  agent: AgentRef
  /** The narrow variant: side panels, no reading column. */
  compact?: boolean
  placeholder?: string
  picker?: React.ReactNode
  /** Text to put in the composer; a new `id` fills it again. */
  fill?: { text: string; id: number }
  /** Under an assistant reply. */
  after?: (m: Msg, last: boolean) => React.ReactNode
  /** Shown while the thread has no messages. */
  empty?: React.ReactNode
  /** Above the composer. */
  above?: React.ReactNode
  /** Run before each message is posted (and its context read). */
  send?: SendOpts
}

/** A chat thread: its messages and cards, a streaming reply, and the composer that sends the next one. */
export function Thread({ id, agent, compact, placeholder, picker, fill, after, empty, above, send: opts }: ThreadProps) {
  const qc = useQueryClient()
  const { messages, busy, reconnecting, next, loadingEarlier, monitoring } = useThread(id)
  const { out, card } = useCredit()
  const can = useCan()
  const bottom = useRef<HTMLDivElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  // Where the view stood when earlier messages were asked for: the height and the message then first.
  const anchor = useRef<{ height: number; first?: string } | null>(null)
  const detail = useSessionDetail(id)
  const { data } = detail
  // A notice streamed after a dismiss is a re-tell (monitoring switched on again): show it.
  const [closed, setClosed] = useState<{ id?: string; monitoring?: Notice }>()
  const notice = closed?.id === id && closed?.monitoring === monitoring ? undefined : monitoring ?? data?.monitoring
  // Gone for this person and agent until monitoring is switched on again; the server keeps it.
  const closeNotice = () => {
    setClosed({ id, monitoring })
    api.post(`/api/chat/sessions/${id}/monitoring/dismiss`).catch(() => setClosed(undefined))
  }
  useEffect(() => {
    if (!data) return
    hydrate(id, data)
    if (data.resume_from !== undefined) resume(qc, id, data.resume_from)
  }, [id, data, qc])
  const first = messages[0]?.id
  // Earlier messages arrive above the view: keep what was on screen where it was.
  useLayoutEffect(() => {
    const at = anchor.current, el = scroller.current
    if (!at || !el || at.first === first) return
    el.scrollTop += el.scrollHeight - at.height
    anchor.current = null
  }, [first])
  useLayoutEffect(() => { if (!loadingEarlier) anchor.current = null }, [loadingEarlier])
  useLayoutEffect(() => { if (stick.current && !anchor.current) bottom.current?.scrollIntoView({ block: "end" }) }, [messages, out])

  const last = messages[messages.length - 1]
  const settledReply = last && last.role === "assistant" && !last.streaming ? last.content : ""
  const pad = compact ? "px-3" : "mx-auto max-w-[720px] px-4"
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div
        ref={scroller}
        className="min-h-0 flex-1 overflow-y-auto"
        onScroll={(e) => {
          const t = e.currentTarget
          stick.current = t.scrollHeight - t.scrollTop - t.clientHeight < 80
          if (t.scrollTop < 200 && next !== undefined && !loadingEarlier) {
            stick.current = false
            anchor.current = { height: t.scrollHeight, first }
            void loadEarlier(id).catch(() => { anchor.current = null })
          }
        }}
      >
        <div className={cn(pad, compact ? "space-y-5 py-4" : "space-y-7 pt-8 pb-10")}>
          {loadingEarlier && <div className="flex justify-center py-1" role="status" aria-label="Loading earlier messages"><Loader2 className="size-4 animate-spin text-muted-foreground" /></div>}
          {!messages.length && !busy && detail.isPending && (
            <div className="space-y-8" role="status" aria-label="Loading">
              <Skeleton className="ml-auto h-11 w-72 rounded-xl" />
              <div className="space-y-2"><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-2/3" /></div>
            </div>
          )}
          {!messages.length && !busy && detail.isError && <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />}
          {!messages.length && !busy && detail.isSuccess && empty}
          {messages.map((m, i) => (m.role === "user" ? <UserMessage key={m.id} m={m} /> : (
            <div key={m.id}>
              <AssistantMessage
                m={m} agent={agent} first={messages[i - 1]?.role !== "assistant"} last={i === messages.length - 1} busy={busy}
                onAnswer={(c, a) => answer(qc, id, c.id, a)} onSwitchBack={() => switchBack(qc, id)}
              />
              {after?.(m, i === messages.length - 1)}
            </div>
          )))}
          <div ref={bottom} />
        </div>
      </div>
      <div className={cn(pad, "w-full space-y-3", compact ? "border-t pt-3 pb-3" : "pb-4")}>
        <div role="status" aria-live="polite" className="sr-only">{settledReply}</div>
        {reconnecting && (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground"><Loader2 className="size-3.5 animate-spin" />Reconnecting</div>
        )}
        {out && card}
        {!out && notice && <MonitoringNotice notice={notice} onDismiss={closeNotice} />}
        {above}
        <Composer
          key={id} onSend={(t, f) => { stick.current = true; void send(qc, id, t, f, opts) }} onStop={() => stop(id)} busy={busy} disabled={out}
          canAttach={can("files.attach")} autoFocus={!compact} fill={fill}
          placeholder={placeholder ?? (out ? "Out of credit" : `Message ${agent.name}`)} picker={picker}
        />
      </div>
    </div>
  )
}


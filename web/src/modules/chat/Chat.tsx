import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2, MoreHorizontal, Plus } from "lucide-react"
import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"

import { AgentAvatar } from "@/components/common"
import { useConfirm } from "@/lib/confirm"
import { creditTone } from "@/lib/credit"
import { Page } from "@/components/shell/Page"
import { RenameDialog } from "@/components/shell/RenameDialog"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { ErrorState, QueryState } from "@/components/QueryState"
import { Skeleton } from "@/components/ui/skeleton"
import { api, ApiError } from "@/lib/api"
import { usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useChatAgents, useMe, useSessions } from "@/lib/hooks"
import type { AgentRef, ChatSession, CreditMeter } from "@/lib/types"
import { AgentPicker } from "./AgentPicker"
import { Refusal } from "./Cards"
import { Composer } from "./Composer"
import { AssistantMessage, UserMessage } from "./Message"
import type { SessionDetail } from "./types"
import { answer, evict, hydrate, loadEarlier, resume, send, stop, useThread } from "./turns"


/** Credit left, and the card that replaces the composer's promise when it is gone. */
function useCredit() {
  const { data: me } = useMe()
  const out = !!me?.credit && me.credit.left_usd <= 0
  const card = me?.credit && (
    <Refusal card={{
      id: "out", kind: "refusal", title: "Your credit has run out.",
      body: `${usd(me.credit.total_usd)} used${me.credit.window === "month" ? " this month" : ""}.`,
      help: me.invited_by ? `Ask ${me.invited_by} to add more.` : "Ask an admin to add credit.",
    }} />
  )
  return { me, out, card, meter: me?.role === "visitor" && me.credit ? me.credit : null }
}

/** Credit left, in one line for the header. */
function HeaderMeter({ meter }: { meter: CreditMeter }) {
  const tone = creditTone(meter)
  const pct = Math.max(0, Math.min(1, meter.left_usd / meter.total_usd))
  return (
    <span className="ml-3 flex items-center gap-2 text-xs lg:hidden">
      <span className={cn("font-medium tabular", tone === "out" && "text-destructive", tone === "low" && "text-warning")}>{usd(meter.left_usd)} left</span>
      <span className="h-1 w-20 overflow-hidden rounded-full bg-foreground/10">
        <span className={cn("block h-full rounded-full", tone === "out" ? "bg-destructive" : tone === "low" ? "bg-warning" : "bg-teal")} style={{ width: `${pct * 100}%` }} />
      </span>
    </span>
  )
}

export function NewChat() {
  const agents = useChatAgents()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { out, card } = useCredit()
  const list = agents.data ?? []
  const agent = list.find((a) => a.id === params.get("agent")) ?? list[0]
  // A failed start clears itself, so the composer is never left disabled.
  const start = useMutation({
    mutationFn: ({ agentId }: { agentId: string; text: string; files: File[] }) => api.post<ChatSession>("/api/chat/sessions", { agent_id: agentId }),   // not idempotent: no retry
    onSuccess: (s, { text, files }) => {
      hydrate(s.id, { messages: [] })
      send(qc, s.id, text, files)
      navigate(`/chat/${s.id}`)
    },
  })
  const go = (text: string, files: File[]) => { if (agent) start.mutate({ agentId: agent.id, text, files }) }
  const pick = (a: AgentRef) => setParams({ agent: a.id }, { replace: true })

  return (
    <Page crumbs={[{ label: "Chat" }, { label: "New chat" }]} flush>
      <div className="mx-auto flex h-full max-w-[720px] flex-col justify-center px-4 pb-[10vh]">
        <QueryState
          query={agents} empty="No agent is available to you. Ask an admin."
          skeleton={<Skeleton className="mx-auto mb-7 h-28 w-48" />}
        >
          {() => agent && (
            <>
              <div className="mb-7 flex flex-col items-center text-center">
                <AgentAvatar src={agent.avatar} name={agent.name} className="size-14 rounded-xl" />
                <h1 className="mt-3 text-2xl font-semibold tracking-tight">{agent.name}</h1>
                <div className="mt-1 flex flex-wrap justify-center gap-x-2 text-sm text-muted-foreground">
                  {agent.description && <span>{agent.description}</span>}
                  {agent.model && <span>{agent.model}</span>}
                </div>
              </div>
              {out && <div className="mb-3">{card}</div>}
              <Composer
                key={agent.id} onSend={go} onStop={() => {}} busy={start.isPending} disabled={out} autoFocus
                placeholder={out ? "Out of credit" : `Message ${agent.name}`}
                picker={<AgentPicker agents={list} value={agent} onChange={pick} />}
              />
              {!out && !!agent.starters?.length && (
                <div className="mt-3 flex flex-wrap justify-center gap-2">
                  {agent.starters.map((s) => (
                    <button key={s} disabled={start.isPending} onClick={() => go(s, [])} className="h-8 rounded-md border px-3 text-sm text-muted-foreground hover:bg-accent hover:text-foreground">{s}</button>
                  ))}
                </div>
              )}
            </>
          )}
        </QueryState>
      </div>
    </Page>
  )
}

export function ChatView() {
  const { id } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { messages, busy, reconnecting, next, loadingEarlier } = useThread(id)
  const { out, card, meter } = useCredit()
  const bottom = useRef<HTMLDivElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  // Where the view stood when earlier messages were asked for: the height and the message then first.
  const anchor = useRef<{ height: number; first?: string } | null>(null)

  // Always fetched, and again on focus: another tab or a restart may have saved a newer reply.
  const detail = useQuery({
    queryKey: ["chat", "session", id],
    queryFn: () => api.get<SessionDetail>(`/api/chat/sessions/${id}`),
    enabled: !!id,
    refetchOnWindowFocus: () => !busy,
    retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2,
  })
  const { data } = detail
  useEffect(() => {
    if (!id || !data) return
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

  const { data: sessions } = useSessions()
  const session = sessions?.find((s) => s.id === id) ?? data?.session
  const agent = session?.agent
  const confirm = useConfirm()
  const [renaming, setRenaming] = useState<ChatSession | null>(null)
  const del = useMutation({
    mutationFn: (s: ChatSession) => api.del(`/api/chat/sessions/${s.id}`),
    onSuccess: (_, s) => {
      evict(s.id)
      qc.removeQueries({ queryKey: ["chat", "session", s.id] })
      qc.invalidateQueries({ queryKey: ["chat", "sessions"] })
      navigate("/chat")
    },
  })
  const remove = async () => {
    if (!session || !(await confirm({ title: "Delete chat?", body: session.title, action: "Delete", destructive: true }))) return
    del.mutate(session)
  }

  if (detail.error instanceof ApiError && detail.error.status === 404) {
    return (
      <Page crumbs={[{ label: "Chat", to: "/chat" }]}>
        <div className="flex flex-col items-center gap-3 py-24">
          <div className="text-sm text-muted-foreground">Chat not found</div>
          <Button variant="outline" size="sm" asChild><Link to="/chat"><Plus />New chat</Link></Button>
        </div>
      </Page>
    )
  }
  const last = messages[messages.length - 1]
  const settledReply = last && last.role === "assistant" && !last.streaming ? last.content : ""
  return (
    <Page
      crumbs={[{ label: "Chat", to: "/chat" }, { label: session?.title ?? "" }]}
      flush
      actions={session && (
        <>
          <span className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:flex">
            <AgentAvatar src={session.agent.avatar} name={session.agent.name} className="size-4 rounded" />
            <span>{session.agent.name}</span>
            {session.agent.model && <span className="text-faint">{session.agent.model}</span>}
          </span>
          {meter && <HeaderMeter meter={meter} />}
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="Chat actions"><MoreHorizontal /></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => setRenaming(session)}>Rename</DropdownMenuItem>
              <DropdownMenuItem variant="destructive" onSelect={remove}>Delete</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )}
    >
      <div className="flex h-full">
        <div className="flex min-w-0 flex-1 flex-col">
          <div
            ref={scroller}
            className="min-h-0 flex-1 overflow-y-auto"
            onScroll={(e) => {
              const t = e.currentTarget
              stick.current = t.scrollHeight - t.scrollTop - t.clientHeight < 80
              if (t.scrollTop < 200 && next !== undefined && !loadingEarlier && id) {
                stick.current = false
                anchor.current = { height: t.scrollHeight, first }
                void loadEarlier(id).catch(() => { anchor.current = null })
              }
            }}
          >
            <div className="mx-auto max-w-[720px] space-y-7 px-4 pt-8 pb-10">
              {loadingEarlier && <div className="flex justify-center py-1" role="status" aria-label="Loading earlier messages"><Loader2 className="size-4 animate-spin text-muted-foreground" /></div>}
              {!messages.length && !busy && detail.isPending && (
                <div className="space-y-8" role="status" aria-label="Loading">
                  <Skeleton className="ml-auto h-11 w-72 rounded-xl" />
                  <div className="space-y-2"><Skeleton className="h-4 w-full" /><Skeleton className="h-4 w-11/12" /><Skeleton className="h-4 w-2/3" /></div>
                </div>
              )}
              {!messages.length && !busy && detail.isError && <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />}
              {agent && messages.map((m, i) => (m.role === "user" ? <UserMessage key={m.id} m={m} /> : (
                <AssistantMessage
                  key={m.id} m={m} agent={agent} first={messages[i - 1]?.role !== "assistant"} last={i === messages.length - 1} busy={busy}
                  onAnswer={(c, a) => answer(qc, id!, c.id, a)}
                />
              )))}
              <div ref={bottom} />
            </div>
          </div>
          <div className="mx-auto w-full max-w-[720px] space-y-3 px-4 pb-4">
            <div role="status" aria-live="polite" className="sr-only">{settledReply}</div>
            {reconnecting && (
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground"><Loader2 className="size-3.5 animate-spin" />Reconnecting</div>
            )}
            {out && card}
            {agent && (
              <Composer
                key={id} onSend={(t, f) => { stick.current = true; send(qc, id!, t, f) }} onStop={() => stop(id!)} busy={busy} disabled={out} autoFocus
                placeholder={out ? "Out of credit" : `Message ${agent.name}`}
                picker={<AgentPicker agents={[agent]} value={agent} />}
              />
            )}
          </div>
        </div>
      </div>
      <RenameDialog session={renaming} onClose={() => setRenaming(null)} />
    </Page>
  )
}

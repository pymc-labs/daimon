import { useMutation, useQueryClient } from "@tanstack/react-query"
import { MoreHorizontal, Plus } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"

import { AgentAvatar } from "@/components/common"
import { useConfirm } from "@/lib/confirm"
import { creditTone } from "@/lib/credit"
import { Page } from "@/components/shell/Page"
import { RenameDialog } from "@/components/shell/RenameDialog"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { QueryState } from "@/components/QueryState"
import { Skeleton } from "@/components/ui/skeleton"
import { api, ApiError } from "@/lib/api"
import { usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useCan, useChatAgents, useMe, useSessions } from "@/lib/hooks"
import type { AgentRef, ChatSession, CreditMeter } from "@/lib/types"
import { Pill } from "../agents/bits"
import { AgentPicker } from "./AgentPicker"
import { ThreadModel } from "./ThreadModel"
import { Composer } from "./Composer"
import { Thread } from "./Thread"
import { useCredit, useSessionDetail } from "./threadHooks"
import { evict, hydrate, send } from "./turns"


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
  const can = useCan()
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
    <Page crumbs={[{ label: "Chat" }, { label: "New thread" }]} flush>
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
                key={agent.id} onSend={go} onStop={() => {}} busy={start.isPending} disabled={out} canAttach={can("files.attach")} autoFocus
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
  const { meter, out } = useCredit()
  const detail = useSessionDetail(id)
  const { data } = detail
  const { data: sessions } = useSessions()
  const session = sessions?.find((s) => s.id === id) ?? data?.session
  const agent = session?.agent
  const confirm = useConfirm()
  const { data: me } = useMe()
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
    if (!session || !(await confirm({ title: "Delete thread?", body: session.title, action: "Delete", destructive: true }))) return
    del.mutate(session)
  }

  if (detail.error instanceof ApiError && detail.error.status === 404) {
    return (
      <Page crumbs={[{ label: "Chat", to: "/chat" }]}>
        <div className="flex flex-col items-center gap-3 py-24">
          <div className="text-sm text-muted-foreground">Thread not found</div>
          <Button variant="outline" size="sm" asChild><Link to="/chat"><Plus />New thread</Link></Button>
        </div>
      </Page>
    )
  }
  return (
    <Page
      crumbs={[{ label: "Chat", to: "/chat" }, { label: session?.title ?? "" }]}
      flush
      actions={session && (
        <>
          <span className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:flex">
            <AgentAvatar src={session.agent.avatar} name={session.agent.name} className="size-4 rounded" />
            <span>{session.agent.name}</span>
          </span>
          <ThreadModel sid={session.id} model={session.agent.model ?? ""} onChanged={() => void detail.refetch()} />
          {me?.role === "visitor" && <Pill>Held to this chat</Pill>}
          {meter && <HeaderMeter meter={meter} />}
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="Thread actions"><MoreHorizontal /></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => setRenaming(session)}>Rename</DropdownMenuItem>
              <DropdownMenuItem variant="destructive" onSelect={remove}>Delete</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )}
    >
      <div className="flex h-full">
        {id && agent && <Thread id={id} agent={agent} placeholder={out ? "Out of credit" : `Message ${agent.name}`} picker={<AgentPicker agents={[agent]} value={agent} />} />}
      </div>
      <RenameDialog session={renaming} onClose={() => setRenaming(null)} />
    </Page>
  )
}

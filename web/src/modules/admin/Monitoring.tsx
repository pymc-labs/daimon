import { useQuery } from "@tanstack/react-query"
import { Check, CircleAlert, Eye } from "lucide-react"
import { Link } from "react-router"

import { Ago, Mono } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Facet, FilterBar } from "@/components/filters"
import { AgentLink } from "@/components/links"
import { PanelHead } from "@/components/panel"
import { LoadMore, QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { rowProps, useParam } from "@/lib/filters"
import { usePaged } from "@/lib/hooks"
import type { AgentRef } from "@/lib/types"
import { cn } from "@/lib/utils"
import { AssistantMessage, UserMessage } from "../chat/Message"
import type { Msg } from "../chat/types"
import { useAgents } from "../agents/api"
import type { AdminPerson, AgentMonitoring, MonitoredConversation, MonitoredEvent, MonitoredWhere } from "./types"

const WHERE: Record<MonitoredWhere, string> = { direct_messages: "Direct messages", web: "Web chat", mcp: "MCP" }
const dateInput = "h-7 rounded-md border bg-background px-2 text-xs outline-none focus:border-border-strong"
const OUTCOME: Record<string, string> = { error: "Failed", denied: "Denied", cancelled: "Cancelled" }

const base = (agent: string) => `/api/admin/monitoring/agents/${encodeURIComponent(agent)}`

function query(filters: Record<string, string>) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(filters)) if (v) p.set(k, v)
  const s = p.toString()
  return s ? `?${s}` : ""
}

export function Monitoring() {
  const agents = useAgents()
  const [chosen, setAgent] = useParam("agent")
  const [person, setPerson] = useParam("person")
  const [where, setWhere] = useParam("where")
  const [from, setFrom] = useParam("from")
  const [to, setTo] = useParam("to")
  const agent = chosen || agents.data?.[0]?.id || ""
  const people = usePaged<AdminPerson>(["admin", "people"], "/api/admin/people")
  return (
    <>
      <FilterBar className="px-0 pt-0">
        <Facet single label="Agent" value={agent ? [agent] : []} onChange={([v]) => setAgent(v ?? null)}
          options={(agents.data ?? []).map((a) => ({ value: a.id, label: a.name }))} />
        <Facet single label="Person" value={person ? [person] : []} onChange={([v]) => setPerson(v ?? null)}
          options={(people.data ?? []).map((p) => ({ value: p.id, label: p.name }))} />
        <Facet single label="Where" value={where ? [where] : []} onChange={([v]) => setWhere(v ?? null)}
          options={Object.entries(WHERE).map(([value, label]) => ({ value, label }))} />
        <input type="date" aria-label="From" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value || null)} className={dateInput} />
        <input type="date" aria-label="To" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value || null)} className={dateInput} />
      </FilterBar>
      {agent && <Conversations agent={agent} filters={{ person, where, from, to }} />}
    </>
  )
}

function Conversations({ agent, filters }: { agent: string; filters: Record<string, string> }) {
  const q = usePaged<MonitoredConversation>(["admin", "monitoring", agent, filters], `${base(agent)}/conversations${query(filters)}`)
  const state = useQuery({ queryKey: ["admin", "monitoring", agent, "state"], queryFn: () => api.get<AgentMonitoring>(base(agent)) })
  const off = state.data && !state.data.direct_messages.on && !state.data.mcp.on
  return (
    <>
      <QueryState query={q} empty={off ? <Off agent={agent} /> : "Nothing kept"}>
        {(data) => <><List data={data} /><LoadMore list={q} /></>}
      </QueryState>
      <Drawer param="conversation" title="Conversation" ids={(q.data ?? []).map((c) => c.id)} wide>
        {(id) => <Transcript agent={agent} conversation={(q.data ?? []).find((c) => c.id === id)} id={id} />}
      </Drawer>
    </>
  )
}

function Off({ agent }: { agent: string }) {
  return (
    <div className="flex flex-col items-center gap-3">
      <span>Monitoring is off for this agent</span>
      <Link to={`/agents/${encodeURIComponent(agent)}?node=monitoring`} className="text-foreground hover:underline">Open monitoring</Link>
    </div>
  )
}

function List({ data }: { data: MonitoredConversation[] }) {
  const [, open] = useParam("conversation")
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="w-28">Last</TableHead><TableHead>Person</TableHead><TableHead>Where</TableHead>
          <TableHead className="w-24 text-right">Questions</TableHead><TableHead className="w-24 text-right max-sm:hidden">Tools</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((c) => (
          <TableRow key={c.id} {...rowProps(() => open(c.id))}>
            <TableCell className="text-muted-foreground"><Ago iso={c.last_at} /></TableCell>
            <TableCell className="font-medium">{c.person.name}</TableCell>
            <TableCell>{WHERE[c.where]}</TableCell>
            <TableCell className="text-right tabular">{c.questions}</TableCell>
            <TableCell className="text-right tabular max-sm:hidden">{c.tools}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** An empty id keeps the chat's rating buttons off a transcript someone else wrote. */
const msg = (e: MonitoredEvent, role: Msg["role"]): Msg => ({ id: "", role, content: e.content ?? "", created_at: e.at })
const noop = () => {}
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })

function ToolRow({ e }: { e: MonitoredEvent }) {
  const bad = e.outcome != null && e.outcome !== "completed"
  return (
    <div className="flex h-8 min-w-0 items-center gap-2 rounded-md border px-3 text-xs">
      {bad ? <CircleAlert className="size-3.5 shrink-0 text-destructive" /> : <Check className="size-3.5 shrink-0 text-success" />}
      <Mono className="min-w-0 truncate">{e.tool}</Mono>
      <span className={cn("ml-auto shrink-0", bad ? "text-destructive" : "text-muted-foreground")}>{OUTCOME[e.outcome ?? ""] ?? "Done"}</span>
      <span className="shrink-0 text-faint tabular">{clock(e.at)}</span>
    </div>
  )
}

/** A kept conversation in order, in the chat's own message components; tool calls are rows. */
function Thread({ events, agent }: { events: MonitoredEvent[]; agent: AgentRef }) {
  return (
    <div className="space-y-5">
      {events.map((e, i) => e.kind === "tool" ? <ToolRow key={e.id} e={e} />
        : e.kind === "question" ? <UserMessage key={e.id} m={msg(e, "user")} />
          : <AssistantMessage key={e.id} m={msg(e, "assistant")} agent={agent} first={events[i - 1]?.kind !== "answer"} last={i === events.length - 1} busy={false} onAnswer={noop} />)}
    </div>
  )
}

function Transcript({ agent, conversation, id }: { agent: string; conversation?: MonitoredConversation; id: string }) {
  const agents = useAgents()
  const found = agents.data?.find((a) => a.id === agent)
  const q = usePaged<MonitoredEvent>(["admin", "monitoring", agent, "transcript", id], `${base(agent)}/conversations/${encodeURIComponent(id)}`)
  const ref: AgentRef = { id: agent, name: found?.name ?? "", avatar: found?.avatar ?? "", model: found?.model_label ?? "", description: found?.purpose ?? "" }
  return (
    <div>
      <PanelHead
        icon={<Eye className="size-4 text-muted-foreground" />}
        title={conversation?.person.name ?? "Conversation"}
        subtitle={conversation && WHERE[conversation.where]}
        tags={found && <AgentLink id={found.id} name={found.name} avatar={found.avatar} />}
      />
      <div className="border-t px-5 py-5">
        <QueryState query={q} empty="Nothing kept">
          {(events) => <><Thread events={events} agent={ref} /><LoadMore list={q} /></>}
        </QueryState>
      </div>
    </div>
  )
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CalendarClock, Pause, Play, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { AgentAvatar, Ago, Prose } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Facet, FilterBar, SearchInput } from "@/components/filters"
import { AgentLink, ChannelLink } from "@/components/links"
import { Facts, PanelHead, PanelSection, Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { rowProps, useListParam, useParam } from "@/lib/filters"
import { absolute, channelLabel, plural } from "@/lib/format"
import { useCan } from "@/lib/hooks"
import { Pill } from "../agents/bits"
import { Editor } from "./Editor"
import { RunNow } from "./RunNow"
import type { Choices, Routine } from "./types"

const LIST = ["routines", "list"]
const CHOICES = ["routines", "choices"]
const STATUS = { active: "Active", paused: "Paused", failed: "Failed" } as const
const ownerOf = (r: Routine) => (r.mine ? "You" : r.owner ?? "Unknown person")
const statusOf = (r: Routine): keyof typeof STATUS => (r.failed ? "failed" : r.enabled ? "active" : "paused")

export function Routines() {
  const can = useCan()
  const [, setRoutine] = useParam("routine")
  const list = useQuery({ queryKey: LIST, queryFn: () => api.get<Routine[]>("/api/routines") })
  const choices = useQuery({ queryKey: CHOICES, queryFn: () => api.get<Choices>("/api/routines/choices") })
  const add = can("routines.manage") && choices.data?.can_create
  return (
    <Page crumbs={[{ label: "Routines" }]} actions={add ? <Button size="sm" onClick={() => setRoutine("new")}><Plus />New routine</Button> : null}>
      <QueryState query={list} empty="No routines">
        {(rows) => <RoutineTable rows={rows} choices={choices.data} />}
      </QueryState>
      {/* An empty list still needs the drawer, for a first routine. */}
      {list.data?.length === 0 && <Drawer param="routine" title="Routine">{(id) => id === "new" && choices.data ? <NewRoutine choices={choices.data} /> : null}</Drawer>}
    </Page>
  )
}

function NewRoutine({ choices }: { choices: Choices }) {
  const [, setRoutine] = useParam("routine")
  return (
    <div className="flex flex-1 flex-col">
      <PanelHead icon={<CalendarClock className="size-5" />} title="New routine" />
      <Editor choices={choices} onDone={(id) => setRoutine(id ?? null)} />
    </div>
  )
}

function Result({ r }: { r: Routine }) {
  if (!r.last_run) return <span className="text-faint">Never</span>
  return (
    <span className="flex items-center gap-2">
      <Ago iso={r.last_run} />
      {r.failed && <Pill className="text-destructive">Failed</Pill>}
    </span>
  )
}

function RoutineTable({ rows: all, choices }: { rows: Routine[]; choices: Choices | undefined }) {
  const [, setRoutine] = useParam("routine")
  const [q, setQ] = useParam("q")
  const [agents, setAgents] = useListParam("agent")
  const [owners, setOwners] = useListParam("owner")
  const [status, setStatus] = useListParam("status")
  const everyone = all.some((r) => !r.mine)
  const rows = all.filter((r) =>
    (!q || `${r.title} ${r.agent.name} ${r.message}`.toLowerCase().includes(q.toLowerCase()))
    && (!agents.length || agents.includes(r.agent.id)) && (!owners.length || owners.includes(ownerOf(r))) && (!status.length || status.includes(statusOf(r))))
  const agentOptions = [...new Map(all.map((r) => [r.agent.id, r.agent.name])).entries()].map(([value, label]) => ({ value, label }))
  const ownerOptions = [...new Set(all.map(ownerOf))].map((o) => ({ value: o, label: o }))
  return (
    <>
      <FilterBar>
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        <Facet label="Agent" value={agents} onChange={setAgents} options={agentOptions} />
        {everyone && <Facet label="Owner" value={owners} onChange={setOwners} options={ownerOptions} />}
        <Facet label="Status" value={status} onChange={setStatus} options={Object.entries(STATUS).map(([value, label]) => ({ value, label }))} />
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "routine")}</span>
      </FilterBar>
      <div className="mx-auto max-w-[1040px] px-4 lg:px-6">
        {!rows.length ? <div className="py-16 text-center text-sm text-muted-foreground">No routines match</div> : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Routine</TableHead><TableHead className="max-sm:hidden">Schedule</TableHead><TableHead className="max-md:hidden">Post to</TableHead>
                <TableHead className="max-sm:hidden">Next run</TableHead><TableHead className="max-lg:hidden">Last run</TableHead>
                {everyone && <TableHead className="max-lg:hidden">Owner</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.id} {...rowProps(() => setRoutine(r.id))}>
                  <TableCell className="max-w-72">
                    <span className="block min-w-0">
                      <span className="flex items-center gap-2"><span className="truncate font-medium">{r.title}</span>{!r.enabled && <Pill>Paused</Pill>}</span>
                      <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground"><AgentAvatar name={r.agent.name} className="size-4 text-[8px]" /><span className="truncate">{r.agent.name}</span></span>
                    </span>
                  </TableCell>
                  <TableCell className="max-sm:hidden">{r.schedule}</TableCell>
                  <TableCell className="max-md:hidden">{r.dm ? "Direct message" : r.destination ? channelLabel(r.destination.name, r.destination.kind) : <span className="text-faint">None</span>}</TableCell>
                  <TableCell className="max-sm:hidden">{r.next_run ? <Ago iso={r.next_run} /> : <span className="text-faint">Paused</span>}</TableCell>
                  <TableCell className="max-lg:hidden"><Result r={r} /></TableCell>
                  {everyone && <TableCell className="text-muted-foreground max-lg:hidden">{ownerOf(r)}</TableCell>}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
      <Drawer param="routine" title="Routine" ids={rows.map((r) => r.id)} wide>
        {(id) => {
          if (id === "new") return choices ? <NewRoutine choices={choices} /> : null
          const r = all.find((x) => x.id === id)
          return r && choices ? <Detail key={r.id} routine={r} choices={choices} /> : null
        }}
      </Drawer>
    </>
  )
}

function Detail({ routine: r, choices }: { routine: Routine; choices: Choices }) {
  const qc = useQueryClient()
  const confirm = useConfirm()
  const can = useCan()
  const [, setRoutine] = useParam("routine")
  const manage = can("routines.manage")
  const refresh = () => qc.invalidateQueries({ queryKey: ["routines"] })
  const toggle = useMutation({
    mutationFn: () => api.post<Routine>(`/api/routines/${r.id}/${r.enabled ? "pause" : "resume"}`),
    onSuccess: () => { void refresh(); toast(r.enabled ? "Paused" : "Resumed") },
  })
  const del = useMutation({
    mutationFn: () => api.del(`/api/routines/${r.id}`),
    onSuccess: () => { setRoutine(null); void refresh(); toast("Routine deleted") },
  })
  return (
    <div className="flex flex-1 flex-col">
      <PanelHead
        icon={<CalendarClock className="size-5" />}
        title={r.title}
        subtitle={r.schedule}
        tags={<>{!r.enabled && <Tag>Paused</Tag>}{r.sandbox_tools && <Tag>Sandbox tools</Tag>}</>}
        actions={manage && (
          <>
            <RunNow routine={r} />
            <Button variant="outline" size="sm" disabled={toggle.isPending} onClick={() => toggle.mutate()}>
              {r.enabled ? <><Pause />Pause</> : <><Play />Resume</>}
            </Button>
          </>
        )}
        menu={manage ? [{
          label: "Delete", icon: <Trash2 />, destructive: true, disabled: del.isPending,
          onSelect: async () => { if (await confirm({ title: "Delete routine?", body: `${r.title}, ${r.schedule}`, action: "Delete", destructive: true })) del.mutate() },
        }] : []}
      />
      <PanelSection>
        <Facts rows={[
          ["Agent", <AgentLink key="agent" id={r.agent.id} name={r.agent.name} />],
          ["Post to", r.dm ? "Direct message" : r.destination ? <ChannelLink key="dest" id={r.destination.id} name={r.destination.name} kind={r.destination.kind} /> : "None"],
          ["Next run", r.next_run ? absolute(r.next_run) : "Paused"],
          ["Last run", r.last_run ? <span key="last" className="flex items-center gap-2">{absolute(r.last_run)}{r.failed && <Pill className="text-destructive">Failed</Pill>}</span> : "Never"],
          ["Owner", r.owner && !r.mine ? r.owner : null],
        ]} />
      </PanelSection>
      {r.last_output && <PanelSection title="Last result"><Prose text={r.last_output} breaks /></PanelSection>}
      {manage && <Editor key={[r.id, r.name, r.agent.id, r.cron, r.timezone, r.message, r.catch_up, r.destination?.id, r.dm].join("|")} routine={r} choices={choices} onDone={(id) => { if (!id) setRoutine(null) }} />}
    </div>
  )
}

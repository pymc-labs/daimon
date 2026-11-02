import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Pause, Play, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { Ago } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { rowProps, useParam } from "@/lib/filters"
import { absolute, channelLabel } from "@/lib/format"
import { useCan } from "@/lib/hooks"
import { Pill } from "../agents/bits"
import { Editor } from "./Editor"
import type { Choices, Routine } from "./types"

const LIST = ["routines", "list"]
const CHOICES = ["routines", "choices"]

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
      {list.data?.length === 0 && <Drawer param="routine" title="New routine">{(id) => id === "new" && choices.data ? <NewRoutine choices={choices.data} /> : null}</Drawer>}
    </Page>
  )
}

function NewRoutine({ choices }: { choices: Choices }) {
  const [, setRoutine] = useParam("routine")
  return <Editor choices={choices} onDone={(id) => setRoutine(id ?? null)} />
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

function RoutineTable({ rows, choices }: { rows: Routine[]; choices: Choices | undefined }) {
  const [, setRoutine] = useParam("routine")
  const everyone = rows.some((r) => !r.mine)
  return (
    <div className="mx-auto max-w-[1040px]">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead>Agent</TableHead><TableHead>Schedule</TableHead><TableHead className="max-md:hidden">Post to</TableHead>
            <TableHead className="max-sm:hidden">Next run</TableHead><TableHead className="max-lg:hidden">Last run</TableHead>
            {everyone && <TableHead className="max-lg:hidden">Owner</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.id} {...rowProps(() => setRoutine(r.id))}>
              <TableCell>
                <span className="flex items-center gap-2"><span className="truncate font-medium">{r.agent.name}</span>{!r.enabled && <Pill>Paused</Pill>}</span>
              </TableCell>
              <TableCell>{r.schedule}</TableCell>
              <TableCell className="max-md:hidden">{r.destination ? channelLabel(r.destination.name) : <span className="text-faint">None</span>}</TableCell>
              <TableCell className="max-sm:hidden">{r.next_run ? <Ago iso={r.next_run} /> : <span className="text-faint">Paused</span>}</TableCell>
              <TableCell className="max-lg:hidden"><Result r={r} /></TableCell>
              {everyone && <TableCell className="text-muted-foreground max-lg:hidden">{r.mine ? "You" : (r.owner ?? "Unknown")}</TableCell>}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Drawer param="routine" title="Routine" ids={rows.map((r) => r.id)} wide>
        {(id) => {
          if (id === "new") return choices ? <NewRoutine choices={choices} /> : null
          const r = rows.find((x) => x.id === id)
          return r && choices ? <Detail key={r.id} routine={r} choices={choices} /> : null
        }}
      </Drawer>
    </div>
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
    <div>
      <div className="px-5 py-5">
        <div className="flex items-center gap-2"><span className="text-base font-semibold">{r.agent.name}</span>{!r.enabled && <Pill>Paused</Pill>}</div>
        <div className="text-sm text-muted-foreground">{r.schedule}</div>
      </div>
      <section className="border-t px-5 py-4">
        <dl className="space-y-1.5 text-sm">
          {([
            ["Next run", r.next_run ? absolute(r.next_run) : "Paused"],
            ["Last run", r.last_run ? `${absolute(r.last_run)}${r.failed ? ", failed" : ""}` : "Never"],
            ...(r.owner && !r.mine ? [["Owner", r.owner] as const] : []),
          ] as const).map(([k, v]) => (
            <div key={k} className="grid grid-cols-[112px_1fr] gap-3"><dt className="text-muted-foreground">{k}</dt><dd>{v}</dd></div>
          ))}
        </dl>
        {r.last_output && <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-accent p-3 text-xs">{r.last_output}</pre>}
      </section>
      {manage && (
        <>
          <div className="border-t"><Editor routine={r} choices={choices} onDone={(id) => { if (!id) setRoutine(null) }} /></div>
          <div className="flex gap-2 border-t px-5 py-4">
            <Button variant="outline" size="sm" disabled={toggle.isPending} onClick={() => toggle.mutate()}>
              {r.enabled ? <><Pause />Pause</> : <><Play />Resume</>}
            </Button>
            <Button variant="outline" size="sm" className="text-destructive" disabled={del.isPending} onClick={async () => {
              if (await confirm({ title: "Delete routine?", body: `${r.agent.name}, ${r.schedule}`, action: "Delete", destructive: true })) del.mutate()
            }}><Trash2 />Delete</Button>
          </div>
        </>
      )}
    </div>
  )
}

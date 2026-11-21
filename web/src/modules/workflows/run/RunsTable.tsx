import { CircleHelp, CircleX, Hourglass, MessageCircleQuestion, UserX, Eye } from "lucide-react"
import { useState } from "react"
import { useLocation, useNavigate } from "react-router"

import { Ago, Money } from "@/components/common"
import { FilterBar, SearchInput } from "@/components/filters"
import { Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { rowProps, useParam } from "@/lib/filters"
import { duration, plural } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useAllRuns, useAttention, useNow, useRuns } from "../api"
import { RUN, TRIGGER } from "../kinds"
import type { AttentionItem, AttentionKind, RunStatus, RunSummary, Workflow } from "../types"
import { RunBadge } from "../ui"
import { monthOf, until } from "./lib"
import { BoardView, CalendarView } from "./RunsBoard"

const live = (r: RunSummary) => r.status === "running" || r.status === "waiting" || r.status === "queued"

/** Runs as rows: status, number, workflow, trigger, started, duration, where it is, cost and version. Rows open the run page. */
export function RunsTable({ runs, showWorkflow }: { runs: RunSummary[]; showWorkflow?: boolean }) {
  const navigate = useNavigate()
  const now = useNow(runs.some(live))
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="w-32 pl-4 lg:pl-6">Status</TableHead>
          <TableHead className="w-24">Run</TableHead>
          {showWorkflow && <TableHead>Workflow</TableHead>}
          <TableHead className="max-md:hidden">Step</TableHead>
          <TableHead className="max-lg:hidden">Trigger</TableHead>
          <TableHead className="max-sm:hidden">Started</TableHead>
          <TableHead className="max-sm:hidden">Duration</TableHead>
          <TableHead className="max-xl:hidden">Version</TableHead>
          <TableHead className="pr-4 text-right lg:pr-6">Cost</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((r) => {
          const t = TRIGGER[r.trigger.kind]
          const took = r.duration_ms ?? (live(r) ? now - Date.parse(r.started_at) : null)
          return (
            <TableRow key={r.id} {...rowProps(() => navigate(`/workflows/${r.workflow.id}/runs/${r.number}`))} className="cursor-pointer">
              <TableCell className="pl-4 lg:pl-6"><RunBadge status={r.status} /></TableCell>
              <TableCell className="tabular"><span className="flex items-center gap-2">{r.number}{r.test && <Tag>Test</Tag>}</span></TableCell>
              {showWorkflow && <TableCell className="max-w-0 w-full"><span className="block truncate font-medium">{r.workflow.name}</span>{r.label && <span className="block truncate text-xs text-muted-foreground">{r.label}</span>}</TableCell>}
              <TableCell className={cn("max-md:hidden", !showWorkflow && "max-w-0 w-full")}>
                <span className={cn("block truncate", r.status === "failed" ? "text-foreground" : "text-muted-foreground")}>{r.step_title ?? (r.status === "succeeded" ? "Done" : "–")}</span>
              </TableCell>
              <TableCell className="max-lg:hidden"><span className="flex items-center gap-1.5 whitespace-nowrap text-muted-foreground"><t.icon className="size-3.5" />{r.triggered_by?.name ?? t.label}</span></TableCell>
              <TableCell className="whitespace-nowrap max-sm:hidden"><Ago iso={r.started_at} /></TableCell>
              <TableCell className="tabular max-sm:hidden">{took == null ? <span className="text-faint">–</span> : duration(took)}</TableCell>
              <TableCell className="tabular text-muted-foreground max-xl:hidden">v{r.version}</TableCell>
              <TableCell className="pr-4 text-right lg:pr-6"><Money usd={r.cost_usd} /></TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

/** A workflow's Runs tab: the list, and a board and a calendar when the spec names stages or a card. */
export function RunsTab({ wf }: { wf: Workflow }) {
  const [view, setView] = useParam("view")
  const [month, setMonth] = useParam("month")
  const [today] = useState(() => monthOf(Date.now()))
  const query = useRuns(wf.id)
  const staged = Object.values(wf.spec.nodes).some((n) => n.stage) || !!wf.spec.card
  const dated = !!wf.spec.card?.date
  const shown = staged && (view === "board" || (view === "calendar" && dated)) ? view : "list"
  return (
    <>
      {staged && (
        <FilterBar>
          <ToggleGroup type="single" value={shown} onValueChange={(x) => x && setView(x === "list" ? null : x)} variant="segmented" size="sm">
            <ToggleGroupItem value="list" className="px-3 text-xs">List</ToggleGroupItem>
            <ToggleGroupItem value="board" className="px-3 text-xs">Board</ToggleGroupItem>
            {dated && <ToggleGroupItem value="calendar" className="px-3 text-xs">Calendar</ToggleGroupItem>}
          </ToggleGroup>
        </FilterBar>
      )}
      {shown === "board" ? <BoardView id={wf.id} />
        : shown === "calendar" ? <CalendarView id={wf.id} month={month || today} onMonth={(m) => setMonth(m === today ? null : m)} />
        : <QueryState query={query} className="px-6 pt-6" isEmpty={(r) => !r.length} empty="No runs">{(runs) => <RunsTable runs={runs} />}</QueryState>}
    </>
  )
}

const STATUSES: RunStatus[] = ["running", "waiting", "succeeded", "failed", "cancelled", "timed_out"]

/** Every run the caller can see, and a Needs attention tab. */
export function RunsPage() {
  const { pathname } = useLocation()
  const attention = useAttention()
  const n = attention.data?.length ?? 0
  return (
    <Page crumbs={[{ label: "Runs" }]} flush tabs={[{ label: "All", to: "/runs", end: true }, { label: n ? `Needs attention ${n}` : "Needs attention", to: "/runs/attention" }]}>
      {pathname.endsWith("/attention") ? <Attention /> : <AllRuns />}
    </Page>
  )
}

function AllRuns() {
  const query = useAllRuns()
  const [q, setQ] = useParam("q")
  const [status, setStatus] = useParam("status")
  const rows = (query.data ?? []).filter((r) => (!status || r.status === status) && (!q || `${r.workflow.name} ${r.label ?? ""} ${r.number}`.toLowerCase().includes(q.toLowerCase())))
  return (
    <>
      <FilterBar>
        <SearchInput value={q} onChange={(x) => setQ(x || null)} className="w-56" />
        <Select value={status || "all"} onValueChange={(v) => setStatus(v === "all" ? null : v)}>
          <SelectTrigger size="sm" className="h-8 w-36 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            {STATUSES.map((s) => { const R = RUN[s]; return <SelectItem key={s} value={s}><R.icon className={R.cls} />{R.label}</SelectItem> })}
          </SelectContent>
        </Select>
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "run")}</span>
      </FilterBar>
      <QueryState query={query} className="px-6" isEmpty={() => !rows.length} empty="No runs">{() => <RunsTable runs={rows} showWorkflow />}</QueryState>
    </>
  )
}

const KIND: Record<AttentionKind, { icon: typeof CircleX; label: string; cls: string }> = {
  failed: { icon: CircleX, label: "Failed", cls: "text-destructive" },
  unknown: { icon: CircleHelp, label: "Unknown outcome", cls: "text-destructive" },
  approval: { icon: Hourglass, label: "Approval", cls: "text-warning" },
  question: { icon: MessageCircleQuestion, label: "Question", cls: "text-warning" },
  needs_owner: { icon: UserX, label: "Needs owner", cls: "text-warning" },
  needs_review: { icon: Eye, label: "Needs review", cls: "text-muted-foreground" },
}

function Attention() {
  const query = useAttention()
  const navigate = useNavigate()
  const open = (a: AttentionItem) => navigate(a.run ? `/workflows/${a.workflow.id}/runs/${a.run.number}${a.step ? `?step=${encodeURIComponent(a.step)}` : ""}` : `/workflows/${a.workflow.id}`)
  return (
    <QueryState query={query} className="px-6 pt-6" isEmpty={(r) => !r.length} empty="Nothing needs attention">{(items) => (
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className="w-44 pl-4 lg:pl-6">Kind</TableHead><TableHead>Workflow</TableHead><TableHead className="w-24 max-sm:hidden">Run</TableHead>
            <TableHead className="max-md:hidden">Detail</TableHead><TableHead className="max-sm:hidden">Since</TableHead><TableHead className="pr-4 text-right lg:pr-6">Expires in</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((a, i) => {
            const K = KIND[a.kind]
            return (
              <TableRow key={i} {...rowProps(() => open(a))} className="cursor-pointer">
                <TableCell className="pl-4 lg:pl-6"><span className={cn("flex items-center gap-1.5 whitespace-nowrap", K.cls)}><K.icon className="size-3.5" /><span className="text-foreground">{K.label}</span></span></TableCell>
                <TableCell className="max-w-0 w-full"><span className="block truncate font-medium">{a.workflow.name}</span></TableCell>
                <TableCell className="tabular max-sm:hidden">{a.run?.number ?? "–"}</TableCell>
                <TableCell className="max-w-80 truncate text-muted-foreground max-md:hidden">{a.detail}</TableCell>
                <TableCell className="whitespace-nowrap max-sm:hidden"><Ago iso={a.since} /></TableCell>
                <TableCell className="pr-4 text-right tabular lg:pr-6">{a.expires_at ? <span className="text-warning">{until(a.expires_at)}</span> : <span className="text-faint">–</span>}</TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    )}</QueryState>
  )
}

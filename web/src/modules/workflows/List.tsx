import { Plus } from "lucide-react"
import { Link, useNavigate } from "react-router"

import { Ago, Avatar, Money } from "@/components/common"
import { FilterBar, SearchInput } from "@/components/filters"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { rowProps, useParam } from "@/lib/filters"
import { plural } from "@/lib/format"
import { useCan } from "@/lib/hooks"
import { useWorkflows } from "./api"
import type { WorkflowSummary } from "./types"
import { StatusBadge, Ticks, TriggerIcons } from "./ui"

const VIEWS: Record<string, { label: string; keep: (w: WorkflowSummary) => boolean }> = {
  all: { label: "All", keep: () => true },
  mine: { label: "Mine", keep: (w) => w.mine },
  shared: { label: "Shared with me", keep: (w) => w.shared_with_me },
  failing: { label: "Failing", keep: (w) => w.failing },
  paused: { label: "Paused", keep: (w) => w.status === "paused" },
  routines: { label: "Routines", keep: (w) => !!w.source_routine },
}

export function WorkflowList() {
  const can = useCan()
  const navigate = useNavigate()
  const query = useWorkflows()
  const [q, setQ] = useParam("q")
  const [view, setView] = useParam("view")
  const v = VIEWS[view ?? "all"] ?? VIEWS.all!
  const rows = (query.data ?? []).filter((w) => v.keep(w) && (!q || `${w.name} ${w.goal} ${w.owner.name}`.toLowerCase().includes(q.toLowerCase())))
  return (
    <Page
      crumbs={[{ label: "Workflows" }]}
      flush
      actions={can("routines.manage") && <Button size="sm" asChild><Link to="/workflows/new"><Plus />New workflow</Link></Button>}
    >
      <FilterBar>
        <ToggleGroup type="single" value={view ?? "all"} onValueChange={(x) => x && setView(x === "all" ? null : x)} variant="segmented" size="sm">
          {Object.entries(VIEWS).map(([k, x]) => <ToggleGroupItem key={k} value={k} className="px-3 text-xs">{x.label}</ToggleGroupItem>)}
        </ToggleGroup>
        <SearchInput value={q} onChange={(x) => setQ(x || null)} className="w-56" />
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "workflow")}</span>
      </FilterBar>
      <QueryState query={query} className="px-6" isEmpty={() => !rows.length} empty="No workflows">{() => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="w-32 pl-4 max-sm:hidden lg:pl-6">Status</TableHead>
              <TableHead className="max-sm:pl-4">Name</TableHead>
              <TableHead className="max-md:hidden">Triggers</TableHead>
              <TableHead className="max-sm:hidden">Last 10 runs</TableHead>
              <TableHead className="max-lg:hidden">Next run</TableHead>
              <TableHead className="max-xl:hidden">Owner</TableHead>
              <TableHead className="pr-4 text-right lg:pr-6">Last 30 days</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((w) => (
              <TableRow key={w.id} {...rowProps(() => navigate(`/workflows/${w.id}`))} className="cursor-pointer">
                <TableCell className="pl-4 max-sm:hidden lg:pl-6"><StatusBadge status={w.status} /></TableCell>
                <TableCell className="max-w-0 w-full max-sm:pl-4">
                  <span className="flex items-center gap-2">
                    <span className="truncate font-medium">{w.name}</span>
                    {w.draft_changes > 0 && w.status !== "draft" && <span className="rounded bg-link/10 px-1.5 text-[11px] font-medium text-link">Draft</span>}
                    {w.source_routine && <span className="rounded border px-1.5 text-[11px] text-muted-foreground">Routine</span>}
                  </span>
                  <span className="block truncate text-xs text-muted-foreground max-sm:hidden">{w.goal}</span>
                  <span className="mt-1 block sm:hidden"><StatusBadge status={w.status} /></span>
                </TableCell>
                <TableCell className="max-md:hidden"><TriggerIcons triggers={w.triggers} /></TableCell>
                <TableCell className="max-sm:hidden"><Ticks ticks={w.ticks} /></TableCell>
                <TableCell className="whitespace-nowrap text-muted-foreground max-lg:hidden">{w.next_run ? <Ago iso={w.next_run} /> : <span className="text-faint">–</span>}</TableCell>
                <TableCell className="max-xl:hidden">
                  <span className="flex items-center gap-2 whitespace-nowrap"><Avatar name={w.owner.name} className="size-5 text-[9px]" />{w.owner.name}</span>
                </TableCell>
                <TableCell className="pr-4 text-right text-muted-foreground lg:pr-6"><Money usd={w.spend_usd} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}</QueryState>
    </Page>
  )
}

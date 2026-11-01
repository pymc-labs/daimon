import { Globe, MessageCircle, Plus } from "lucide-react"
import { Link, useNavigate } from "react-router"

import { BrandIcon } from "@/components/brand"
import { Ago, AgentAvatar, Avatar, Money } from "@/components/common"
import { Facet, FilterBar, SearchInput } from "@/components/filters"
import { rowProps, useListParam, useParam } from "@/lib/filters"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { plural } from "@/lib/format"
import { useCan, useMe } from "@/lib/hooks"
import { useAgents } from "./api"
import type { AgentSummary } from "./types"

const RUNS: Record<AgentSummary["runs_in"][number], { label: string; icon: React.ReactNode }> = {
  discord: { label: "Discord channels", icon: <BrandIcon brand="discord" className="size-3.5" /> },
  slack: { label: "Slack channels", icon: <BrandIcon brand="slack" className="size-3.5" /> },
  teams: { label: "Teams channels", icon: <BrandIcon brand="teams" className="size-3.5" /> },
  dm: { label: "Direct messages", icon: <MessageCircle className="size-3.5 text-muted-foreground" /> },
  web: { label: "Web chat", icon: <Globe className="size-3.5 text-muted-foreground" /> },
}
const PROVIDER = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google", selfhosted: "Self-hosted" }

export function AgentList() {
  const { data: me } = useMe()
  const can = useCan()
  const navigate = useNavigate()
  const query = useAgents()
  const { data } = query
  const [q, setQ] = useParam("q")
  const [mine, setMine] = useParam("mine")
  const [runs, setRuns] = useListParam("runs")
  const rows = (data ?? []).filter((a) =>
    (!mine || a.owner.id === me?.id || a.co_owners.some((p) => p.id === me?.id))
    && (!q || `${a.name} ${a.purpose} ${a.owner.name}`.toLowerCase().includes(q.toLowerCase()))
    && (!runs.length || runs.some((r) => a.runs_in.includes(r as AgentSummary["runs_in"][number]))))
  return (
    <Page
      crumbs={[{ label: "Agents" }]}
      flush
      actions={can("agents.create") && <Button size="sm" asChild><Link to="/agents/new"><Plus />New agent</Link></Button>}
    >
      <FilterBar>
        <ToggleGroup type="single" value={mine ? "mine" : "all"} onValueChange={(v) => v && setMine(v === "mine" ? "1" : null)} variant="segmented" size="sm">
          <ToggleGroupItem value="all" className="px-3 text-xs">All</ToggleGroupItem>
          <ToggleGroupItem value="mine" className="px-3 text-xs">Mine</ToggleGroupItem>
        </ToggleGroup>
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        <Facet label="Runs in" value={runs} onChange={setRuns} options={(["discord", "dm", "web"] as const).map((k) => ({ value: k, label: RUNS[k].label, icon: RUNS[k].icon }))} />
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "agent")}</span>
      </FilterBar>
      <QueryState query={query} className="px-6" isEmpty={() => !rows.length} empty="No agents">{() => (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-4 lg:pl-6">Name</TableHead>
                <TableHead className="max-md:hidden">Model</TableHead>
                <TableHead>Runs in</TableHead>
                <TableHead className="max-lg:hidden">Owner</TableHead>
                <TableHead className="text-right max-sm:hidden">This month</TableHead>
                <TableHead className="pr-4 text-right lg:pr-6">Edited</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((a) => (
                <TableRow key={a.id} {...rowProps(() => navigate(`/agents/${a.id}`))} className="cursor-pointer">
                  <TableCell className="pl-4 lg:pl-6">
                    <span className="flex items-center gap-3">
                      <AgentAvatar src={a.avatar} name={a.name} className="size-7" />
                      <span className="min-w-0">
                        <span className="flex items-center gap-2">
                          <span className="truncate font-medium">{a.name}</span>
                          {a.draft_changes > 0 && <span className="rounded bg-link/10 px-1.5 text-[11px] font-medium text-link">Draft</span>}
                        </span>
                        <span className="block truncate text-xs text-muted-foreground">{a.purpose}</span>
                      </span>
                    </span>
                  </TableCell>
                  <TableCell className="max-md:hidden">
                    {a.model_label}
                    {a.backend !== "anthropic" && <span className="ml-1.5 text-xs text-muted-foreground">{PROVIDER[a.backend]}</span>}
                  </TableCell>
                  <TableCell>
                    <span className="flex items-center gap-2">
                      {a.runs_in.map((k) => (
                        <Tooltip key={k}>
                          <TooltipTrigger asChild><span className="flex">{RUNS[k].icon}</span></TooltipTrigger>
                          <TooltipContent>{RUNS[k].label}</TooltipContent>
                        </Tooltip>
                      ))}
                      {!a.runs_in.length && <span className="text-faint">–</span>}
                    </span>
                  </TableCell>
                  <TableCell className="max-lg:hidden">
                    <span className="flex items-center gap-2"><Avatar name={a.owner.name} className="size-5 text-[9px]" /><span className="truncate">{a.owner.name}</span></span>
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground max-sm:hidden"><Money usd={a.month_usd} /></TableCell>
                  <TableCell className="pr-4 text-right text-muted-foreground lg:pr-6">
                    <span className="inline-flex items-center gap-1.5">
                      <span className="max-xl:hidden">{a.updated_by.name.split(" ")[0]}</span><Ago iso={a.updated_at} />
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}</QueryState>
    </Page>
  )
}

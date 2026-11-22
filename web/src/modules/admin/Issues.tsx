import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowDown, ExternalLink } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"

import { Ago } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Facet, FilterBar, SearchInput } from "@/components/filters"
import { AgentLink, PersonLink, RoutineLink } from "@/components/links"
import { Facts, PanelHead, PanelSection, Tag } from "@/components/panel"
import { LoadMore, QueryState, Rows } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useAgents } from "@/modules/agents/api"
import { api, qs } from "@/lib/api"
import { deepLink } from "@/lib/deep-links"
import { rowProps, useParam } from "@/lib/filters"
import { absolute, exact } from "@/lib/format"
import { useCan, usePaged } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { AREAS, bucketLabel, REFRESH_MS, usePeriod } from "./ops"
import { IssueName, PeriodToggle, SeverityIcon, Updated } from "./ops-ui"
import type { Issue, IssueAction, IssueDetail, IssueEvent, OpsPeriod } from "./types"

const STATUS = { active: "Active", quiet: "Quiet", resolved: "Resolved", muted: "Muted" }
const SEVERITY = { error: "Error", warning: "Warning" }
const opts = (m: Record<string, string>) => Object.entries(m).map(([value, label]) => ({ value, label }))
const one = (set: (v: string | null) => void) => ([v]: string[]) => set(v ?? null)

export function Issues() {
  const [period] = usePeriod()
  const [status, setStatus] = useParam("status")
  const [area, setArea] = useParam("area")
  const [severity, setSeverity] = useParam("severity")
  const [agent, setAgent] = useParam("agent")
  const [q, setQ] = useParam("q")
  const [sort, setSort] = useParam("sort")
  const agents = useAgents()
  const filters = { period, status, area, severity, agent, q, sort }
  const list = usePaged<Issue>(["admin", "issues", filters], "/api/admin/issues" + qs(filters), { refetchInterval: REFRESH_MS })
  return (
    <>
      <FilterBar className="px-0 pt-0">
        <PeriodToggle />
        <Facet single label="Status" value={[status || "active"]} onChange={([v]) => setStatus(v && v !== "active" ? v : null)} options={opts(STATUS)} />
        <Facet single label="Area" value={area ? [area] : []} onChange={one(setArea)} options={opts(AREAS)} />
        <Facet single label="Severity" value={severity ? [severity] : []} onChange={one(setSeverity)} options={opts(SEVERITY)} />
        <Facet single label="Agent" value={agent ? [agent] : []} onChange={one(setAgent)} options={(agents.data ?? []).map((a) => ({ value: a.id, label: a.name }))} />
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-48" />
        <Updated at={list.updatedAt || undefined} />
      </FilterBar>
      <QueryState query={list} empty="No issues">{(rows) => (
        <>
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Issue</TableHead><TableHead className="max-md:hidden">Area</TableHead><TableHead className="max-lg:hidden">Subject</TableHead>
                <TableHead className="max-md:hidden">Trend</TableHead>
                <SortHead label="Events" on={sort === "events"} onClick={() => setSort("events")} className="max-md:hidden" />
                <TableHead className="text-right max-md:hidden">People</TableHead><TableHead className="text-right max-lg:hidden">First seen</TableHead>
                <SortHead label="Last seen" on={!sort} onClick={() => setSort(null)} />
              </TableRow>
            </TableHeader>
            <TableBody>{rows.map((i) => <IssueRow key={i.fingerprint} issue={i} period={period} end={list.updatedAt} />)}</TableBody>
          </Table>
          <LoadMore list={list} />
        </>
      )}</QueryState>
      <Drawer param="issue" title="Issue" ids={(list.data ?? []).map((i) => i.fingerprint)} wide>{(fp) => <IssueView fingerprint={fp} />}</Drawer>
    </>
  )
}

function SortHead({ label, on, onClick, className }: { label: string; on: boolean; onClick: () => void; className?: string }) {
  return (
    <TableHead className={cn("text-right", className)} aria-sort={on ? "descending" : undefined}>
      <button type="button" onClick={onClick} className={cn("inline-flex items-center gap-1 hover:text-foreground", on && "text-foreground")}>
        {on && <ArrowDown className="size-3" />}{label}
      </button>
    </TableHead>
  )
}

function IssueRow({ issue: i, period, end }: { issue: Issue; period: OpsPeriod; end: number }) {
  const [, setIssue] = useParam("issue")
  return (
    <TableRow {...rowProps(() => setIssue(i.fingerprint))}>
      <TableCell className="w-full max-w-0 md:w-[32%]"><IssueName issue={i} /></TableCell>
      <TableCell className="text-muted-foreground max-md:hidden">{AREAS[i.area]}</TableCell>
      <TableCell className="max-w-44 truncate text-muted-foreground max-lg:hidden">{i.subject?.name}</TableCell>
      <TableCell className="max-md:hidden"><Spark buckets={i.buckets} period={period} end={end} /></TableCell>
      <TableCell className="text-right tabular max-md:hidden">{exact(i.events)}</TableCell>
      <TableCell className="text-right tabular max-md:hidden">{exact(i.people)}</TableCell>
      <TableCell className="text-right text-muted-foreground max-lg:hidden"><Ago iso={i.first_seen} /></TableCell>
      <TableCell className="text-right text-muted-foreground"><Ago iso={i.last_seen} /></TableCell>
    </TableRow>
  )
}

const SPAN_H: Record<OpsPeriod, number> = { "24h": 1, "7d": 24, "30d": 24 }

/** Events per bucket in one hue, the last bucket ending at `end`; each column is a full-height hover target. */
function Spark({ buckets, period, end }: { buckets: number[]; period: OpsPeriod; end: number }) {
  const [hover, setHover] = useState<string | null>(null)
  const max = Math.max(1, ...buckets)
  function show(i: number) {
    setHover(`${bucketLabel(new Date(end - (buckets.length - i) * SPAN_H[period] * 3_600_000), period)}: ${buckets[i]}`)
  }

  return (
    <Tooltip open={hover != null}>
      <TooltipTrigger asChild>
        <span className="flex h-6 w-24 items-stretch gap-px" onMouseLeave={() => setHover(null)} aria-hidden>
          {buckets.map((n, i) => (
            <span key={i} className="flex flex-1 items-end" onMouseEnter={() => show(i)}>
              <span className={cn("w-full rounded-t-[1px]", n ? "bg-chart-1" : "bg-border")} style={{ height: n ? `${Math.max(12, (n / max) * 100)}%` : 1 }} />
            </span>
          ))}
        </span>
      </TooltipTrigger>
      {hover && <TooltipContent>{hover}</TooltipContent>}
    </Tooltip>
  )
}

function IssueView({ fingerprint }: { fingerprint: string }) {
  const path = `/api/admin/issues/${encodeURIComponent(fingerprint)}`
  const q = useQuery({ queryKey: ["admin", "issue", fingerprint], queryFn: () => api.get<IssueDetail>(path), refetchInterval: REFRESH_MS })
  return <QueryState query={q} className="p-5" skeleton={<div className="p-5"><Rows n={4} /></div>}>{(i) => <IssueBody issue={i} path={path} />}</QueryState>
}

function Subject({ issue: i }: { issue: IssueDetail }) {
  const s = i.subject
  if (!s) return null
  if (s.kind === "routine") return <RoutineLink id={s.id} name={s.name} />
  if (s.kind === "agent") return <AgentLink id={s.id} name={s.name} />
  return s.name
}

function IssueBody({ issue: i, path }: { issue: IssueDetail; path: string }) {
  const qc = useQueryClient()
  const can = useCan()
  const events = usePaged<IssueEvent>(["admin", "issue", i.fingerprint, "events"], `${path}/events`)
  const change = useMutation({
    mutationFn: (action: IssueAction) => api.post<Issue>(`${path}/state`, { action }),
    onSuccess: (_, action) => {
      void qc.invalidateQueries({ queryKey: ["admin", "issue", i.fingerprint] })
      void qc.invalidateQueries({ queryKey: ["admin", "issues"] })
      void qc.invalidateQueries({ queryKey: ["admin", "overview"] })
      toast({ resolve: "Issue resolved", mute: "Issue muted", reopen: "Issue reopened" }[action])
    },
  })
  const s = i.subject
  const open = s?.id && (s.kind === "routine" || s.kind === "agent")
    ? { label: s.kind === "routine" ? "Open routine" : "Open agent", to: s.kind === "routine" ? deepLink("routine", null, s.id) : deepLink("agent", s.id) } : null
  const states: [IssueAction, string][] = !can("workspace.manage") ? [] : i.state === "open" ? [["mute", "Mute"], ["resolve", "Resolve"]] : [["reopen", "Reopen"]]
  return (
    <div className="flex min-h-full flex-1 flex-col">
      <PanelHead
        icon={<SeverityIcon severity={i.severity} />}
        title={i.title}
        tags={<><Tag>{SEVERITY[i.severity]}</Tag><Tag>{AREAS[i.area]}</Tag>{i.regressed && <Tag>Regressed</Tag>}{i.state !== "open" && <Tag>{STATUS[i.state]}</Tag>}</>}
      />
      <PanelSection>
        <Facts rows={[
          ["Subject", <Subject key="s" issue={i} />],
          ["Reason", i.reason_label],
          ["Agent", i.agent && <AgentLink key="a" id={i.agent.id} name={i.agent.name} />],
          ["First seen", absolute(i.first_seen)],
          ["Last seen", <Ago key="l" iso={i.last_seen} />],
          ["Events", exact(i.events)],
          ["People", i.people.length ? <span key="p" className="flex flex-wrap gap-x-3 gap-y-1">{i.people.map((p) => <PersonLink key={p.id} id={p.id} name={p.name} />)}</span> : null],
          ["Release", i.release],
          [STATUS[i.state === "muted" ? "muted" : "resolved"], i.state_by && <span key="by">{i.state_by.name}, <Ago iso={i.state_at} /></span>],
        ]} />
      </PanelSection>
      <PanelSection title="Events">
        <QueryState query={events} empty="No events" skeleton={<Rows n={3} />}>{(rows) => (
          <Table>
            <TableBody>
              {rows.map((e, n) => (
                <TableRow key={`${e.at}-${n}`} className="hover:bg-transparent">
                  <TableCell className="w-20 text-muted-foreground first:pl-0 lg:first:pl-0"><Ago iso={e.at} /></TableCell>
                  <TableCell className="max-w-32 truncate max-sm:hidden">{e.person ? <PersonLink id={e.person.id} name={e.person.name} /> : null}</TableCell>
                  <TableCell className="max-w-32 truncate text-muted-foreground max-sm:hidden">{e.where_label}</TableCell>
                  <TableCell className={cn("text-muted-foreground", e.note ? "whitespace-normal" : "max-w-40 truncate")}>{e.note ?? e.reason_label}</TableCell>
                  <TableCell className="w-10 text-right text-muted-foreground tabular">{e.count > 1 ? `×${e.count}` : ""}</TableCell>
                  <TableCell className="w-28 text-right last:pr-0 lg:last:pr-0">
                    {e.thread_url && <a href={e.thread_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-link hover:underline">Open thread<ExternalLink className="size-3" /></a>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}</QueryState>
        <LoadMore list={events} />
      </PanelSection>
      {(open || states.length > 0) && (
        <div className="sticky bottom-0 mt-auto flex justify-end gap-2 border-t bg-popover px-5 py-3 max-sm:[&>*]:flex-1">
          {open && <Button variant={states.length ? "outline" : "default"} asChild><Link to={open.to}>{open.label}</Link></Button>}
          {states.map(([action, label], n) => (
            <Button key={action} variant={n === states.length - 1 ? "default" : "outline"} disabled={change.isPending} onClick={() => change.mutate(action)}>{label}</Button>
          ))}
        </div>
      )}
    </div>
  )
}

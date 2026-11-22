import { useQuery } from "@tanstack/react-query"
import { ChevronRight, CircleX } from "lucide-react"
import { Link, useNavigate } from "react-router"
import { Bar, BarChart, CartesianGrid, Rectangle, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis, type BarShapeProps } from "recharts"

import { Ago, Money, StatStrip } from "@/components/common"
import { Facts } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { FilterBar } from "@/components/filters"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { Section } from "@/modules/credits/shared"
import { api } from "@/lib/api"
import { rowProps } from "@/lib/filters"
import { duration, exact, usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { AREAS, bucketLabel, REFRESH_MS, usePeriod } from "./ops"
import { IssueName, PeriodToggle, StatusLabel, Updated } from "./ops-ui"
import type { Issue, OpsOverview, OpsPeriod } from "./types"

export function Overview() {
  const [period] = usePeriod()
  const q = useQuery({
    queryKey: ["admin", "overview", period], queryFn: () => api.get<OpsOverview>(`/api/admin/overview?period=${period}`), refetchInterval: REFRESH_MS,
  })
  return (
    <div className="mx-auto max-w-[1040px]">
      <FilterBar className="px-0 pt-0 lg:px-0"><PeriodToggle /><Updated at={q.data?.updated_at} /></FilterBar>
      <QueryState query={q}>{(data) => <OverviewBody data={data} period={period} />}</QueryState>
    </div>
  )
}

const pct = (r: number) => `${(r * 100).toFixed(r < 0.1 ? 1 : 0)}%`
const Alarm = ({ on, children }: { on: boolean; children: React.ReactNode }) =>
  on ? <span className="inline-flex items-center gap-1.5 text-destructive"><CircleX className="size-4" />{children}</span> : <>{children}</>

function OverviewBody({ data, period }: { data: OpsOverview; period: OpsPeriod }) {
  const issues = (area: string) => `/admin/issues?area=${area}${period === "24h" ? "" : `&period=${period}`}`
  const w = data.workspace, f = data.answers_facts
  const share = (
    <Tooltip>
      <TooltipTrigger asChild><span><Alarm on={data.failed_rate >= 0.05}>{pct(data.failed_rate)}</Alarm></span></TooltipTrigger>
      <TooltipContent>{exact(data.failed)} of {exact(data.answers)}</TooltipContent>
    </Tooltip>
  )
  return (
    <>
      <StatStrip stats={[
        { label: "Status", value: <StatusLabel status={data.status} /> },
        { label: "Answers", value: exact(data.answers) },
        { label: "Failed answers", value: share, to: issues("turn") },
        { label: "DM failures", value: exact(data.dm_failures), to: issues("dm") },
        { label: "Routines failing", value: `${exact(data.routines_failing)} of ${exact(data.routines_total)}`, to: "/routines?status=failed" },
        { label: "Credits left", value: <Alarm on={data.runway_days != null && data.runway_days < 7}><Money usd={data.balance_usd} /></Alarm>, to: "/credits" },
      ]} />
      <div className="grid gap-x-6 lg:grid-cols-3">
        <Section title={`Answers per ${period === "24h" ? "hour" : "day"}`} aside={<Legend />} className="lg:col-span-2"><AnswersChart data={data.series} period={period} /></Section>
        <Section title="Failures by area"><AreaBars rows={data.by_area} href={issues} /></Section>
      </div>
      <Section title="Needs attention" aside={<Link to="/admin/issues" className="inline-flex items-center gap-0.5 hover:text-foreground">All issues<ChevronRight className="size-3.5" /></Link>}>
        <Attention rows={data.attention} />
      </Section>
      <div className="grid gap-x-6 md:grid-cols-2">
        <Section title="Answers">
          <div className="rounded-lg border px-4 py-3">
            <Facts wide rows={[
              ["Median time", f.p50_ms == null ? "–" : duration(f.p50_ms)],
              ["Slowest 5%", f.p95_ms == null ? "–" : duration(f.p95_ms)],
              ["Turned away busy", exact(f.busy)],
              ["Recovered by retry", exact(f.recovered)],
              ["Cost per answer", usd(f.cost_per_answer_usd)],
            ]} />
          </div>
        </Section>
        <Section title="Workspace">
          <div className="rounded-lg border px-4 py-3">
            <Facts wide rows={[
              ["Agents", <Link key="a" to="/agents" className="hover:underline">{exact(w.agents)}</Link>],
              ["People", <Link key="p" to="/admin/people" className="hover:underline">{exact(w.people)}</Link>],
              ["Channels", exact(w.channels)],
              ["Routines on", `${exact(w.routines_on)} of ${exact(w.routines_total)}`],
              ["Watches on", `${exact(w.watches_on)} of ${exact(w.watches_total)}`],
              ["Watches failing", exact(w.watches_failing)],
              ["Funding", w.funding_mode === "prepaid" ? "Prepaid" : "Paid by the operator"],
            ]} />
          </div>
        </Section>
      </div>
    </>
  )
}

const Swatch = ({ cls, label }: { cls: string; label: string }) => <span className="inline-flex items-center gap-1.5"><span className={cn("size-2 rounded-[2px]", cls)} />{label}</span>
const Legend = () => <span className="flex gap-3"><Swatch cls="bg-destructive" label="Failed" /><Swatch cls="bg-faint" label="Answered" /></span>

// The answered segment drops 2px at its base, so a surface gap separates it from the failed one below.
const AnsweredShape = (p: BarShapeProps) => {
  const gap = (p.payload as { failed: number }).failed > 0 ? 2 : 0
  return <Rectangle {...p} height={Math.max(0, (p.height ?? 0) - gap)} radius={[4, 4, 0, 0]} />
}

function AnswersChart({ data, period }: { data: OpsOverview["series"]; period: OpsPeriod }) {
  const rows = data.map((d) => ({ ...d, label: bucketLabel(new Date(d.at), period), total: d.answered + d.failed }))
  return (
    <div className="h-56 rounded-lg border px-2 pt-4 pb-1">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} barCategoryGap={2} margin={{ top: 4, right: 20, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey="label" tickLine={false} axisLine={false} interval={period === "7d" ? 0 : period === "24h" ? 3 : 4} tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
          <YAxis tickLine={false} axisLine={false} width={44} allowDecimals={false} tick={{ fontSize: 11, fill: "var(--muted-foreground)" }} />
          <ChartTooltip
            cursor={{ fill: "var(--accent)" }}
            content={({ active, payload }) => {
              const r = active ? (payload?.[0]?.payload as (typeof rows)[number] | undefined) : undefined
              return r ? (
                <div className="rounded-md border bg-popover px-2.5 py-1.5 text-xs shadow-md">
                  <div className="text-muted-foreground">{r.label}</div>
                  <Facts rows={[["Answered", exact(r.answered)], ["Failed", exact(r.failed)], ["Failed share", r.total ? pct(r.failed / r.total) : "–"]]} />
                </div>
              ) : null
            }}
          />
          <Bar dataKey="failed" stackId="a" fill="var(--destructive)" maxBarSize={24} isAnimationActive={false} />
          <Bar dataKey="answered" stackId="a" fill="var(--faint)" maxBarSize={24} isAnimationActive={false} shape={AnsweredShape} />
        </BarChart>
      </ResponsiveContainer>
      <table className="sr-only">
        <caption>Answers</caption>
        <thead><tr><th>Time</th><th>Answered</th><th>Failed</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.at}><td>{r.label}</td><td>{r.answered}</td><td>{r.failed}</td></tr>)}</tbody>
      </table>
    </div>
  )
}

function AreaBars({ rows, href }: { rows: OpsOverview["by_area"]; href: (area: string) => string }) {
  const sorted = [...rows].sort((a, b) => b.events - a.events)
  const max = Math.max(1, ...sorted.map((r) => r.events))
  return (
    <div className="flex h-56 flex-col justify-center gap-0.5 rounded-lg border px-2 py-2">
      {sorted.map((r) => (
        <Link key={r.area} to={href(r.area)} className="grid h-7 grid-cols-[104px_1fr] items-center gap-2 rounded-md px-2 text-xs hover:bg-accent">
          <span className="truncate text-muted-foreground">{r.label}</span>
          <span className="flex items-center gap-1.5">
            {r.events > 0 && <span className="h-3 rounded-r-[4px] bg-chart-1" style={{ width: `max(4px, calc(${(r.events / max) * 100}% - 2.5rem))` }} />}
            <span className="font-medium tabular">{exact(r.events)}</span>
          </span>
        </Link>
      ))}
    </div>
  )
}

function Attention({ rows }: { rows: Issue[] }) {
  const navigate = useNavigate()
  if (!rows.length) return <div className="rounded-lg border py-10 text-center text-sm text-muted-foreground">Nothing needs attention</div>
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>Issue</TableHead><TableHead className="max-md:hidden">Area</TableHead><TableHead className="max-md:hidden">Subject</TableHead>
          <TableHead className="text-right max-md:hidden">Events</TableHead><TableHead className="text-right max-md:hidden">People</TableHead><TableHead className="text-right">Last seen</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((i) => (
          <TableRow key={i.fingerprint} {...rowProps(() => navigate(`/admin/issues?issue=${encodeURIComponent(i.fingerprint)}`))}>
            <TableCell className="w-full max-w-0 md:w-[40%]"><IssueName issue={i} /></TableCell>
            <TableCell className="text-muted-foreground max-md:hidden">{AREAS[i.area]}</TableCell>
            <TableCell className="max-w-48 truncate text-muted-foreground max-md:hidden">{i.subject?.name}</TableCell>
            <TableCell className="text-right tabular max-md:hidden">{exact(i.events)}</TableCell>
            <TableCell className="text-right tabular max-md:hidden">{exact(i.people)}</TableCell>
            <TableCell className="text-right text-muted-foreground"><Ago iso={i.last_seen} /></TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

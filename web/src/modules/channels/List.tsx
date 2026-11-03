import { Fragment, useMemo } from "react"

import { Ago, Money } from "@/components/common"
import { Facet, FilterBar, SearchInput } from "@/components/filters"
import { Drawer } from "@/components/Drawer"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { rowProps, useListParam, useParam } from "@/lib/filters"
import { plural, usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Tag } from "@/modules/admin/shared"
import { useChannels } from "./api"
import { Defaults } from "./Defaults"
import { ACCESS_FILTERS, agentsOf, filterChannels, groupChannels, nameOf, platformLabel, TAGS } from "./lib"
import { ChannelPanel } from "./Panel"
import { PlatformIcon } from "./shared"
import type { Channel } from "./types"

function Budget({ c }: { c: Channel }) {
  const b = c.budget
  if (!b) return <span className="text-faint">–</span>
  return (
    <div className="w-28 max-sm:w-20">
      <div className={cn("text-xs tabular", b.exceeded ? "font-medium text-destructive" : "text-muted-foreground")}>{usd(b.spent_usd)} of {usd(Number(b.limit_usd))}</div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-foreground/10">
        <div className={cn("h-full rounded-full", b.exceeded ? "bg-destructive" : b.percent_used >= 80 ? "bg-warning" : "bg-teal")} style={{ width: `${Math.min(100, b.percent_used)}%` }} />
      </div>
    </div>
  )
}

export function Channels() {
  const query = useChannels()
  const [q, setQ] = useParam("q")
  const [platforms, setPlatforms] = useListParam("platform")
  const [access, setAccess] = useListParam("access")
  const [budget, setBudget] = useListParam("budget")
  const [agents, setAgents] = useListParam("agent")
  const all = useMemo(() => query.data?.channels ?? [], [query.data])
  const rows = useMemo(() => filterChannels(all, { q, platforms, access, budget, agents }), [all, q, platforms, access, budget, agents])
  const groups = useMemo(() => groupChannels(rows), [rows])
  const [, setOpen] = useParam("channel")
  const platformNames = [...new Set(all.map((c) => c.platform))]
  const drawerIds = groups.flatMap((g) => g.rows.map((c) => c.id))

  return (
    <Page crumbs={[{ label: "Channels" }]} flush>
      <Defaults />
      <FilterBar className="pt-0">
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        {platformNames.length > 1 && (
          <Facet label="Platform" value={platforms} onChange={setPlatforms} options={platformNames.map((p) => ({ value: p, label: platformLabel(p), icon: <PlatformIcon platform={p} className="size-3.5" /> }))} />
        )}
        <Facet label="Access" value={access} onChange={setAccess} options={ACCESS_FILTERS} />
        <Facet label="Budget" value={budget} onChange={setBudget} options={[{ value: "yes", label: "Has budget" }, { value: "no", label: "No budget" }]} />
        <Facet label="Agent" value={agents} onChange={setAgents} options={agentsOf(all).map((a) => ({ value: a, label: a }))} />
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "channel")}</span>
      </FilterBar>
      <QueryState query={query} className="px-6">{() => (
        <>
          {!rows.length ? <div className="py-16 text-center text-sm text-muted-foreground">No channels</div> : (
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-4 lg:pl-6">Channel</TableHead>
                  <TableHead className="max-md:hidden">Answers</TableHead>
                  <TableHead className="max-lg:hidden">Access</TableHead>
                  <TableHead>Budget</TableHead>
                  <TableHead className="text-right max-sm:hidden">This month</TableHead>
                  <TableHead className="pr-4 text-right lg:pr-6 max-sm:hidden">Last active</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {groups.map((g) => (
                  <Fragment key={g.key}>
                    {g.name && (
                      <TableRow className="bg-muted/40 hover:bg-muted/40">
                        <TableCell colSpan={6} className="py-1.5 pl-4 text-xs font-medium text-muted-foreground lg:pl-6">{g.name}</TableCell>
                      </TableRow>
                    )}
                    {g.rows.map((c) => (
                      <TableRow key={c.id} {...rowProps(() => setOpen(c.id))} className="cursor-pointer">
                        <TableCell className="pl-4 lg:pl-6">
                          <span className="flex min-w-0 items-center gap-2.5">
                            <PlatformIcon platform={c.platform} className="size-4 shrink-0 text-muted-foreground" />
                            <span className="min-w-0">
                              <span className="block truncate font-medium">{nameOf(c)}</span>
                              <span className="block truncate text-xs text-muted-foreground md:hidden">{c.agent.value ?? "None"}</span>
                            </span>
                          </span>
                        </TableCell>
                        <TableCell className="max-md:hidden">
                          <span className={cn("block max-w-44 truncate", c.agent.source !== "channel" && "text-muted-foreground")}>{c.agent.value ?? "None"}</span>
                        </TableCell>
                        <TableCell className="max-lg:hidden">
                          <span className="flex flex-wrap gap-1">{c.tags.map((t) => <Tag key={t}>{TAGS[t]}</Tag>)}</span>
                        </TableCell>
                        <TableCell><Budget c={c} /></TableCell>
                        <TableCell className="text-right text-muted-foreground max-sm:hidden"><Money usd={c.spend.month_usd} /></TableCell>
                        <TableCell className="pr-4 text-right text-muted-foreground lg:pr-6 max-sm:hidden"><Ago iso={c.last_active_at} /></TableCell>
                      </TableRow>
                    ))}
                  </Fragment>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}</QueryState>
      <Drawer param="channel" title="Channel" ids={drawerIds} wide>{(id) => <ChannelPanel id={id} />}</Drawer>
    </Page>
  )
}

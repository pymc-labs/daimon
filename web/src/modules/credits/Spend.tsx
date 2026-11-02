import { useQuery } from "@tanstack/react-query"
import { CalendarClock } from "lucide-react"
import { BrandIcon } from "@/components/brand"
import { AgentAvatar, Avatar } from "@/components/common"
import { FilterBar } from "@/components/filters"
import { useParam } from "@/lib/filters"
import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, qs } from "@/lib/api"
import { monthName, usd } from "@/lib/format"
import type { SpendBy, SpendRow } from "./types"

const PLATFORM_ICON: Record<string, string> = { discord: "discord", web: "web", slack: "slack", teams: "teams" }

export function SpendTab() {
  const [by, setBy] = useParam("by")
  const [period, setPeriod] = useParam("period")
  const b = (by ?? "person") as SpendBy
  const q = useQuery({
    queryKey: ["credits", "spend", b, period], queryFn: () => api.get<SpendRow[]>(`/api/credits/spend${qs({ by: b, period })}`),
    placeholderData: (p) => p,
  })
  const { data } = q
  const max = Math.max(...(data ?? []).map((r) => r.usd), 1)
  const total = (data ?? []).reduce((s, r) => s + r.usd, 0)
  return (
    <div className="mx-auto max-w-[1040px]">
      <FilterBar className="px-0 pt-0">
        <ToggleGroup type="single" value={b} onValueChange={(v) => v && setBy(v === "person" ? null : v)} variant="segmented" size="sm">
          <ToggleGroupItem value="person" className="px-3 text-xs">People</ToggleGroupItem>
          <ToggleGroupItem value="agent" className="px-3 text-xs">Agents</ToggleGroupItem>
          <ToggleGroupItem value="channel" className="px-3 text-xs">Channels</ToggleGroupItem>
        </ToggleGroup>
        <ToggleGroup type="single" value={period ?? "month"} onValueChange={(v) => v && setPeriod(v === "month" ? null : v)} variant="segmented" size="sm">
          <ToggleGroupItem value="month" className="px-3 text-xs">{monthName()}</ToggleGroupItem>
          <ToggleGroupItem value="last" className="px-3 text-xs">{monthName(1)}</ToggleGroupItem>
        </ToggleGroup>
        <span className="ml-auto text-xs text-muted-foreground tabular">{usd(total)} total</span>
      </FilterBar>
      <QueryState query={q} empty="No spend yet">{(rows) => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>{{ person: "Person", agent: "Agent", channel: "Channel" }[b]}</TableHead>
              <TableHead className="text-right max-sm:hidden">Replies</TableHead>
              <TableHead className="w-[38%] max-md:hidden" />
              <TableHead className="text-right">Spend</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.id}>
                <TableCell>
                  <span className="flex items-center gap-2.5">
                    {b === "agent" ? <AgentAvatar src={r.avatar} name={r.name} />
                      : b === "person" ? <Avatar name={r.name} />
                        : r.kind === "routine" ? <span className="flex size-6 items-center justify-center"><CalendarClock className="size-4 text-muted-foreground" /></span>
                          : <span className="flex size-6 items-center justify-center"><BrandIcon brand={PLATFORM_ICON[r.kind!] ?? "web"} /></span>}
                    <span className="truncate font-medium">{r.name}</span>
                    {r.kind === "Visitor" && <span className="rounded border px-1.5 text-[11px] text-muted-foreground">Visitor</span>}
                  </span>
                </TableCell>
                <TableCell className="text-right text-muted-foreground tabular max-sm:hidden">{r.turns}</TableCell>
                <TableCell className="max-md:hidden">
                  <div className="h-1.5 rounded-full bg-chart-1" style={{ width: `${(r.usd / max) * 100}%` }} />
                </TableCell>
                <TableCell className="text-right font-medium tabular">{usd(r.usd)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}</QueryState>
    </div>
  )
}

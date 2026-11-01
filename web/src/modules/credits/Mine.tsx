import { useQuery } from "@tanstack/react-query"
import { BrandIcon } from "@/components/brand"
import { Ago, AgentAvatar } from "@/components/common"
import { CreditMeter } from "@/components/CreditMeter"
import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { shortDate, usd } from "@/lib/format"
import { useMe } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import type { Mine } from "./types"
import { Section } from "./shared"

export function MineTab() {
  const q = useQuery({ queryKey: ["credits", "mine"], queryFn: () => api.get<Mine>("/api/credits/mine") })
  return <QueryState query={q}>{(data) => <MineBody data={data} />}</QueryState>
}

function MineBody({ data }: { data: Mine }) {
  const { data: me } = useMe()
  const meter = me?.credit
  if (!meter && !data.by_agent.length && !data.turns.length) return <div className="py-16 text-center text-sm text-muted-foreground">No spend yet</div>
  const max = Math.max(...data.by_agent.map((r) => r.usd), 1)
  return (
    <div className="mx-auto max-w-[1040px]">
      {meter && (
        <div className="rounded-lg border p-4">
          <CreditMeter meter={meter} />
          {meter.resets_at && <div className="mt-2 text-xs text-muted-foreground">Resets {shortDate(meter.resets_at)}</div>}
        </div>
      )}
      <div className={cn("grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]", meter && "mt-8")}>
        <Section title="By agent" className="mt-0">
          <div className="divide-y rounded-lg border">
            {data.by_agent.map((r) => (
              <div key={r.id} className="flex h-11 items-center gap-2.5 px-3 text-sm">
                <AgentAvatar src={r.avatar} name={r.name} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{r.name}</span>
                  <span className="mt-1 block h-1 rounded-full bg-chart-1" style={{ width: `${(r.usd / max) * 100}%` }} />
                </span>
                <span className="w-16 text-right font-medium tabular">{usd(r.usd)}</span>
              </div>
            ))}
          </div>
        </Section>
        <Section title="Recent replies" className="mt-0">
          <Table>
            <TableBody>
              {data.turns.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="w-20 text-muted-foreground"><Ago iso={t.at} /></TableCell>
                  <TableCell>
                    <span className="flex min-w-0 items-center gap-2">
                      <AgentAvatar src={t.agent.avatar} name={t.agent.name} className="size-5" />
                      <span className="truncate">{t.title}</span>
                    </span>
                  </TableCell>
                  <TableCell className="w-8"><BrandIcon brand={t.platform} className="text-muted-foreground" /></TableCell>
                  <TableCell className="w-16 text-right tabular">{usd(t.usd)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Section>
      </div>
    </div>
  )
}

import { useQuery } from "@tanstack/react-query"
import { ThumbsDown, ThumbsUp } from "lucide-react"

import { Facet, FilterBar } from "@/components/filters"
import { AgentLink } from "@/components/links"
import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { useParam } from "@/lib/filters"
import { Pill } from "../agents/bits"
import type { Feedback as Data } from "./types"

const FEW = "Fewer than 3"
const PERIODS = [{ value: "7", label: "Last 7 days" }, { value: "30", label: "Last 30 days" }, { value: "90", label: "Last 90 days" }]

export function Feedback() {
  const [period, setPeriod] = useParam("days")
  const days = period || "30"
  const q = useQuery({ queryKey: ["admin", "feedback", days], queryFn: () => api.get<Data>(`/api/admin/feedback?days=${days}`) })
  return (
    <>
      <FilterBar className="px-0 pt-0">
        <Facet single label="Period" value={[days]} onChange={([v]) => setPeriod(v && v !== "30" ? v : null)} options={PERIODS} />
      </FilterBar>
      <QueryState query={q} isEmpty={(d) => !d.agents.length} empty="No feedback in this period">
        {(d) => (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Agent</TableHead>
                <TableHead className="w-28"><ThumbsUp className="size-3.5" aria-label="Thumbs up" /></TableHead>
                <TableHead className="w-28"><ThumbsDown className="size-3.5" aria-label="Thumbs down" /></TableHead>
                <TableHead className="max-sm:hidden">Reasons</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.agents.map((a) => (
                <TableRow key={a.id ?? "unknown"}>
                  <TableCell className="font-medium"><AgentLink id={a.id} name={a.name} /></TableCell>
                  <TableCell>{a.up ?? FEW}</TableCell>
                  <TableCell>{a.down ?? FEW}</TableCell>
                  <TableCell className="max-sm:hidden">
                    <span className="flex flex-wrap gap-1">{a.reasons.map((r) => <Pill key={r.code}>{r.label} {r.count ?? FEW}</Pill>)}</span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </QueryState>
    </>
  )
}

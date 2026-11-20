import { useState } from "react"

import { Money } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { usd } from "@/lib/format"
import type { Arm, Estimate } from "./types"

const LABEL = { agent: "Agent turns", simulator: "Simulator", judge: "Judges", compile: "Compile", sandbox: "Sandbox" } as const
export const suggestedCap = (e: Estimate) => Math.ceil(e.high_usd * 1.2 * 100) / 100

/** The estimate by line, low, expected and high. */
export function EstimateView({ estimate, arms = [] }: { estimate: Estimate; arms?: Arm[] }) {
  const stat = (label: string, v: number) => <div className="px-4 py-2.5"><div className="text-xs text-muted-foreground">{label}</div><div className="text-lg font-semibold tabular">{usd(v)}</div></div>
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 divide-x rounded-lg border">{stat("Low", estimate.low_usd)}{stat("Expected", estimate.expected_usd)}{stat("High", estimate.high_usd)}</div>
      <Table>
        <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Line</TableHead><TableHead className="text-right">Items</TableHead><TableHead className="text-right">Expected</TableHead></TableRow></TableHeader>
        <TableBody>
          {estimate.lines.map((l, i) => (
            <TableRow key={i}>
              <TableCell>{LABEL[l.category]}{l.arm_id && <span className="text-muted-foreground"> · {arms.find((a) => a.id === l.arm_id)?.label ?? l.arm_id}</span>}</TableCell>
              <TableCell className="text-right tabular">{l.items ?? ""}</TableCell>
              <TableCell className="text-right"><Money usd={l.expected_usd} /></TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

/** Approve with a spend cap. Nothing runs until a cap is set. */
export function ApproveBar({ estimate, label = "Approve and run", pending, onApprove }: { estimate: Estimate; label?: string; pending?: boolean; onApprove: (cap: number) => void }) {
  const [text, setText] = useState("")
  const cap = Number(text)
  const ok = text.trim() !== "" && Number.isFinite(cap) && cap > 0
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <label htmlFor="cap" className="text-sm text-muted-foreground">Spend cap</label>
      <div className="relative w-28">
        <span className="pointer-events-none absolute inset-y-0 left-2.5 flex items-center text-sm text-muted-foreground">$</span>
        <Input id="cap" inputMode="decimal" value={text} onChange={(e) => setText(e.target.value)} className="h-8 pl-6 tabular" />
      </div>
      <Button variant="ghost" size="sm" onClick={() => setText(String(suggestedCap(estimate)))}>Use {usd(suggestedCap(estimate))}</Button>
      <Button size="sm" disabled={!ok || pending} onClick={() => onApprove(cap)}>{label}</Button>
    </div>
  )
}

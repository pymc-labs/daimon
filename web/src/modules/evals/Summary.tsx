import { useState } from "react"

import { Money } from "@/components/common"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Pill } from "../agents/bits"
import { pct } from "./bits"
import type { Arm, PairSummary, RunSummary } from "./types"

const H = ({ children }: { children: React.ReactNode }) => <h3 className="mb-2 text-sm font-medium">{children}</h3>
const points = (v: number | null) => (v == null ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(1)}`)

/** The paired difference with its interval, the zero line and the non-inferiority margin. */
function Interval({ p }: { p: PairSummary }) {
  if (p.difference == null || p.low == null || p.high == null) return null
  const span = Math.max(Math.abs(p.low), Math.abs(p.high), p.margin) * 1.25
  const x = (v: number) => 100 + (v / span) * 100
  return (
    <svg viewBox="0 0 200 20" role="img" aria-label={`Difference ${points(p.difference)} points, interval ${points(p.low)} to ${points(p.high)}`} className="h-5 w-44 text-foreground">
      <line x1={x(0)} x2={x(0)} y1="2" y2="18" stroke="var(--border-strong)" />
      <line x1={x(-p.margin)} x2={x(-p.margin)} y1="4" y2="16" stroke="var(--warning)" strokeDasharray="2 2" />
      <line x1={x(p.low)} x2={x(p.high)} y1="10" y2="10" stroke="currentColor" strokeWidth="2" />
      <circle cx={x(p.difference)} cy="10" r="3.5" fill="currentColor" />
    </svg>
  )
}

const verdict = (p: PairSummary) => p.non_inferior === true ? <Pill className="text-success">Not worse</Pill> : p.non_inferior === false ? <Pill className="text-destructive">Worse or unclear</Pill> : <Pill>No verdict</Pill>

/** Pass rates, paired differences, preferences, judge agreement and splits of a run. */
export function Summary({ s, arms }: { s: RunSummary; arms: Arm[] }) {
  const label = (id: string) => arms.find((a) => a.id === id)?.label ?? id
  const dims = Object.keys(s.arms[0]?.breakdown ?? {})
  const [dim, setDim] = useState(dims[0] ?? "")
  const buckets = [...new Set(s.arms.flatMap((a) => Object.keys(a.breakdown[dim] ?? {})))]
  const k = (m: Record<string, number>) => { const keys = Object.keys(m).map(Number).sort((a, b) => a - b); return keys.length ? keys.at(-1)! : null }
  return (
    <div className="space-y-8">
      <section>
        <H>Arms</H>
        <Table>
          <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Arm</TableHead><TableHead className="text-right">Pass rate</TableHead><TableHead className="text-right max-sm:hidden">Rubric</TableHead><TableHead className="text-right max-md:hidden">Repeats</TableHead><TableHead className="text-right">Cost</TableHead></TableRow></TableHeader>
          <TableBody>
            {s.arms.map((a) => {
              const n = k(a.pass_at_k)
              return (
                <TableRow key={a.arm_id}>
                  <TableCell className="font-medium">{a.label}{a.errors > 0 && <span className="ml-2 text-xs text-destructive">{a.errors} errors</span>}</TableCell>
                  <TableCell className="text-right tabular">{pct(a.pass_rate)}<span className="text-muted-foreground">, {a.passed}/{a.scored}</span></TableCell>
                  <TableCell className="text-right tabular max-sm:hidden">{a.rubric_score == null ? "–" : a.rubric_score.toFixed(2)}</TableCell>
                  <TableCell className="text-right text-muted-foreground tabular max-md:hidden">{n && n > 1 ? `pass@${n} ${pct(a.pass_at_k[n])}, pass^${n} ${pct(a.pass_pow_k[n])}` : "–"}</TableCell>
                  <TableCell className="text-right"><Money usd={a.cost_usd} /></TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </section>
      {!!s.comparisons.length && (
        <section>
          <H>Differences</H>
          <Table>
            <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Pair</TableHead><TableHead className="text-right">Points</TableHead><TableHead className="max-sm:hidden">Interval</TableHead><TableHead>Verdict</TableHead></TableRow></TableHeader>
            <TableBody>
              {s.comparisons.map((p) => (
                <TableRow key={`${p.candidate}:${p.reference}`}>
                  <TableCell>{label(p.candidate)}<span className="text-muted-foreground"> vs {label(p.reference)}</span><span className="block text-xs text-muted-foreground tabular">{p.cases} cases, margin {points(-p.margin)}</span></TableCell>
                  <TableCell className="text-right tabular">{points(p.difference)}<span className="block text-xs text-muted-foreground">{points(p.low)} to {points(p.high)}</span></TableCell>
                  <TableCell className="max-sm:hidden"><Interval p={p} /></TableCell>
                  <TableCell>{verdict(p)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      )}
      {!!s.preferences.length && (
        <section>
          <H>Preferences</H>
          <ul className="space-y-1 text-sm">{s.preferences.map((p) => <li key={`${p.grader}:${p.arm_a}:${p.arm_b}`} className="tabular"><span className="text-muted-foreground">{p.grader[0]!.toUpperCase() + p.grader.slice(1)}, </span>{label(p.arm_a)} {p.a_wins}<span className="text-muted-foreground">, ties {p.ties}, </span>{label(p.arm_b)} {p.b_wins}</li>)}</ul>
        </section>
      )}
      {!!s.calibration.length && (
        <section>
          <H>Judge agreement</H>
          <Table>
            <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Judge</TableHead><TableHead className="text-right">Scored by both</TableHead><TableHead className="text-right">Agreement</TableHead><TableHead className="text-right">Kappa</TableHead><TableHead /></TableRow></TableHeader>
            <TableBody>
              {s.calibration.map((c) => (
                <TableRow key={c.judge}>
                  <TableCell>{c.judge}</TableCell><TableCell className="text-right tabular">{c.n}</TableCell><TableCell className="text-right tabular">{pct(c.agreement)}</TableCell>
                  <TableCell className="text-right tabular">{c.kappa == null ? "–" : c.kappa.toFixed(2)}</TableCell><TableCell>{c.flagged && <Pill className="text-warning">Low</Pill>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      )}
      {dims.length > 0 && (
        <section>
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-medium">Breakdown</h3>
            <Select value={dim} onValueChange={setDim}><SelectTrigger aria-label="Split by" className="h-8 w-36"><SelectValue /></SelectTrigger><SelectContent>{dims.map((d) => <SelectItem key={d} value={d}>{d.replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase())}</SelectItem>)}</SelectContent></Select>
          </div>
          <Table>
            <TableHeader><TableRow className="hover:bg-transparent"><TableHead>{dim.replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase())}</TableHead>{s.arms.map((a) => <TableHead key={a.arm_id} className="text-right">{a.label}</TableHead>)}</TableRow></TableHeader>
            <TableBody>
              {buckets.map((b) => (
                <TableRow key={b}>
                  <TableCell>{b}</TableCell>
                  {s.arms.map((a) => { const r = a.breakdown[dim]?.[b]; return <TableCell key={a.arm_id} className="text-right tabular">{r ? <>{pct(r.pass_rate)}<span className="text-muted-foreground">, {r.items}</span></> : "–"}</TableCell> })}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      )}
      {s.notes.map((n) => <p key={n} className="text-xs text-muted-foreground">{n}</p>)}
    </div>
  )
}

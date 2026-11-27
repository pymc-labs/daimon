import { CompareFlags, compareTables, TableDiff as Highlighter, TableView } from "daff"
import { useMemo } from "react"

import { Rows } from "@/components/QueryState"
import { cn } from "@/lib/utils"
import type { Output } from "../types"
import { useGrid } from "./Table"

const MARK: Record<string, string> = { "+++": "bg-success/15", "---": "bg-destructive/15", "->": "bg-warning/15" }

/** Cell by cell difference of two tables, rows and columns that moved or came and went included. */
export function TableDiff({ a, b }: { a: Output; b: Output }) {
  const left = useGrid(a), right = useGrid(b)
  const rows = useMemo(() => {
    const x = left.sheets?.[0]?.rows, y = right.sheets?.[0]?.rows
    if (!x || !y) return null
    const out: string[][] = []
    new Highlighter(compareTables(new TableView(x), new TableView(y)).align(), new CompareFlags()).hilite(new TableView(out))
    return out
  }, [left.sheets, right.sheets])
  if (!rows) return <Rows n={3} />
  if (rows.every((r) => !(r[0] ?? "") || r[0] === "@@")) return <div className="py-6 text-center text-sm text-muted-foreground">No differences</div>
  return (
    <div className="max-h-80 overflow-auto rounded-md border">
      <table className="w-full border-collapse text-xs">
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={cn(MARK[r[0] ?? ""], r[0] === "@@" && "bg-muted font-medium")}>
              {r.slice(1).map((v, c) => <td key={c} className="whitespace-nowrap border-l px-2 py-1">{String(v ?? "")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

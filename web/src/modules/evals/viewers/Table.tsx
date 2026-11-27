import { useState } from "react"

import { Rows } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { Output, Pin } from "../types"
import { type Sheet, useSheets, useText } from "./blob"

const col = (i: number): string => (i < 26 ? String.fromCharCode(65 + i) : col(Math.floor(i / 26) - 1) + col(i % 26))
const cell = (r: number, c: number) => `${col(c)}${r + 1}`

/** Minimal CSV: quoted fields, doubled quotes, CRLF. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = []
  let row: string[] = [], field = "", quoted = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]!
    if (quoted) { if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++ } else quoted = false } else field += c }
    else if (c === '"') quoted = true
    else if (c === ",") { row.push(field); field = "" }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(field); rows.push(row); row = []; field = "" }
    else field += c
  }
  if (field || row.length) { row.push(field); rows.push(row) }
  return rows
}

/** CSV and workbooks as a grid; click a cell, shift-click to extend, then comment on the range. */
export function useGrid(o: Output): { sheets?: Sheet[]; loading: boolean } {
  const csv = o.media_type.includes("csv") || o.name.endsWith(".csv")
  const text = useText(csv ? o.blob.sha256 : undefined)
  const book = useSheets(o, !csv)
  if (csv) return { sheets: text.data === undefined ? undefined : [{ name: o.name, rows: parseCsv(text.data) }], loading: text.data === undefined }
  return { sheets: book.data, loading: book.data === undefined }
}

export function Table({ o, pin }: { o: Output; pin?: Pin }) {
  const { sheets, loading } = useGrid(o)
  const [tab, setTab] = useState(0)
  const [from, setFrom] = useState<[number, number] | null>(null)
  const [to, setTo] = useState<[number, number] | null>(null)
  if (loading || !sheets) return <Rows n={3} />
  const sheet = sheets[tab] ?? sheets[0]!
  const [r0, r1] = from && to ? [Math.min(from[0], to[0]), Math.max(from[0], to[0])] : [-1, -1]
  const [c0, c1] = from && to ? [Math.min(from[1], to[1]), Math.max(from[1], to[1])] : [-1, -1]
  const range = from && to ? (r0 === r1 && c0 === c1 ? cell(r0, c0) : `${cell(r0, c0)}:${cell(r1, c1)}`) : null
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1">
        {sheets.length > 1 && sheets.map((s, i) => <Button key={s.name} size="xs" variant={i === tab ? "secondary" : "ghost"} onClick={() => { setTab(i); setFrom(null); setTo(null) }}>{s.name}</Button>)}
        {range && pin && <Button size="xs" variant="outline" className="ml-auto" onClick={() => pin({ target: "table_range", ref: o.id, sheet: sheet.name, cells: range })}>Comment on {range}</Button>}
      </div>
      <div className="max-h-80 overflow-auto rounded-md border">
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 bg-muted"><tr><th className="w-8 border-b" />{sheet.rows[0]?.map((_, c) => <th key={c} className="border-b border-l px-2 py-1 font-medium text-muted-foreground">{col(c)}</th>)}</tr></thead>
          <tbody>
            {sheet.rows.map((row, r) => (
              <tr key={r}>
                <td className="bg-muted px-1.5 text-right text-faint tabular">{r + 1}</td>
                {row.map((v, c) => (
                  <td key={c} className={cn("cursor-cell whitespace-nowrap border-l border-t px-2 py-1", r >= r0 && r <= r1 && c >= c0 && c <= c1 && "bg-link/15")}
                    onClick={(e) => { if (e.shiftKey && from) setTo([r, c]); else { setFrom([r, c]); setTo([r, c]) } }}>{v}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

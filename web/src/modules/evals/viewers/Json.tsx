import { create } from "jsondiffpatch"
import { format } from "jsondiffpatch/formatters/html"
import "jsondiffpatch/formatters/styles/html.css"
import "./viewers.css"
import { useMemo, useState } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"

import { Rows } from "@/components/QueryState"
import type { Output } from "../types"
import { useText } from "./blob"

const differ = create({ arrays: { detectMove: false } })
const parse = (s: string | undefined): unknown => { try { return s === undefined ? undefined : JSON.parse(s) } catch { return s } }

export function JsonTree({ value, depth = 0, name }: { value: unknown; depth?: number; name?: string }) {
  const [open, setOpen] = useState(depth < 2)
  const label = name !== undefined && <span className="text-muted-foreground">{name}: </span>
  if (value === null || typeof value !== "object") return <div className="pl-4 break-words">{label}<span className={typeof value === "string" ? "text-success" : "text-link"}>{JSON.stringify(value)}</span></div>
  const entries = Object.entries(value)
  return (
    <div className={depth ? "pl-4" : ""}>
      <button className="flex items-center gap-1 text-left" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}{label}<span className="text-faint">{Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`}</span>
      </button>
      {open && entries.map(([k, v]) => <JsonTree key={k} name={k} value={v} depth={depth + 1} />)}
    </div>
  )
}

/** Tool payloads and JSON files as a tree; with a second one, the structural difference. */
export function Json({ o, other }: { o: Output; other?: Output }) {
  const a = useText(o.blob.sha256), b = useText(other?.blob.sha256)
  const html = useMemo(() => {
    if (!other || a.data === undefined || b.data === undefined) return ""
    const left = parse(a.data), right = parse(b.data)
    const delta = differ.diff(left, right)
    return delta ? format(delta, left) : ""
  }, [a.data, b.data, other])
  if (a.data === undefined || (other && b.data === undefined)) return <Rows n={3} />
  if (other) return html ? <div className="max-h-96 overflow-auto rounded-md border p-2" dangerouslySetInnerHTML={{ __html: html }} /> : <div className="py-6 text-center text-sm text-muted-foreground">No differences</div>
  return <div className="max-h-96 overflow-auto rounded-md border p-2 font-mono text-xs leading-5"><JsonTree value={parse(a.data)} /></div>
}

/** Any value as a tree, for payloads already in memory. */
export const Payload = ({ value }: { value: unknown }) => <div className="max-h-72 overflow-auto rounded-md border bg-muted/40 p-2 font-mono text-xs leading-5"><JsonTree value={value} /></div>

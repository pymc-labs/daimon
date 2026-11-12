import { Input } from "@/components/ui/input"
import { fieldType } from "../spec"
import type { Field as SchemaField } from "../types"
import { AddButton, Pick, RemoveButton } from "./parts"
import { useLocal } from "./useLocal"

const TYPES: [string, SchemaField][] = [["text", { type: "string" }], ["number", { type: "number" }], ["yes/no", { type: "boolean" }], ["list", { type: "array" }], ["object", { type: "object" }]]
const typeOf = (f: SchemaField) => (f.enum ? "choice" : fieldType(f))

function Name({ name, onRename, ro }: { name: string; onRename: (to: string) => void; ro: boolean }) {
  const [v, setV] = useLocal(name, () => {})
  return <Input value={v} readOnly={ro} onChange={(e) => setV(e.target.value.replace(/[^\w]/g, "_"))} onBlur={() => v && v !== name && onRename(v)} className="h-8 font-mono text-[12px]" />
}

/** Output fields as rows: name, type; a choice keeps its options. */
export function OutputTable({ fields, onChange, ro }: { fields: Record<string, SchemaField>; onChange: (f: Record<string, SchemaField>) => void; ro: boolean }) {
  const rows = Object.entries(fields)
  const put = (fn: (e: [string, SchemaField][]) => [string, SchemaField][]) => onChange(Object.fromEntries(fn(rows)))
  return (
    <div className="space-y-1.5">
      {rows.length > 0 && (
        <div className="grid grid-cols-[minmax(0,1fr)_112px_28px] gap-1.5 text-xs text-muted-foreground">
          <span>Field</span><span>Type</span><span />
        </div>
      )}
      {rows.map(([k, f], i) => (
        <div key={k} className="space-y-1">
          <div className="grid grid-cols-[minmax(0,1fr)_112px_28px] items-center gap-1.5">
            <Name name={k} ro={ro} onRename={(to) => put((e) => e.map((x, j) => (j === i ? [to, x[1]] : x)))} />
            <Pick value={typeOf(f)} ro={ro} onChange={(t) => put((e) => e.map((x, j) => (j === i ? [k, t === "choice" ? { enum: f.enum ?? ["yes", "no"] } : { ...TYPES.find(([n]) => n === t)![1], description: f.description }] : x)))}
              options={[...TYPES.map(([n]) => ({ value: n, label: n })), { value: "choice", label: "choice" }]} />
            {!ro ? <RemoveButton onClick={() => put((e) => e.filter((_, j) => j !== i))} /> : <span />}
          </div>
          {f.enum && <div className="flex flex-wrap gap-1">{f.enum.map((o) => <span key={o} className="inline-flex h-5 items-center rounded border bg-background px-1.5 font-mono text-[11px] text-muted-foreground">{o}</span>)}</div>}
        </div>
      ))}
      {!ro && <AddButton label="Add field" onClick={() => put((e) => [...e, [`field_${e.length + 1}`, { type: "string" }]])} />}
    </div>
  )
}

import { ArrowRight, Plus, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { cn } from "@/lib/utils"
import { titleOf } from "../spec"
import type { Catalog, Node, Spec } from "../types"
import { KIND } from "../kinds"

export type EditorProps = {
  id: string; n: Node; spec: Spec; catalog?: Catalog; ro: boolean
  /** Change this step on a copy of the spec; `delay` debounces the save while typing. */
  set: (fn: (n: Node, s: Spec) => void, delay?: number) => void
}

export function Section({ title, action, children, className }: { title?: string; action?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("border-b px-5 py-4 last:border-b-0", className)}>
      {(title || action) && (
        <div className="mb-3 flex min-h-6 items-center gap-2">
          <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
          <div className="ml-auto flex items-center gap-1">{action}</div>
        </div>
      )}
      {children}
    </section>
  )
}

export function Field({ label, children, top }: { label: string; children: React.ReactNode; top?: boolean }) {
  return (
    <div className={cn("grid grid-cols-[112px_minmax(0,1fr)] gap-3 text-sm", top ? "items-start" : "items-center")}>
      <Label className={cn("font-normal text-muted-foreground", top && "min-h-8")}>{label}</Label>
      {children}
    </div>
  )
}


export function Segmented<T extends string>({ value, options, onChange, ro }: { value: T; options: [T, string][]; onChange: (v: T) => void; ro?: boolean }) {
  return (
    <ToggleGroup type="single" value={value} onValueChange={(v) => v && !ro && onChange(v as T)} variant="segmented" size="sm" disabled={ro}>
      {options.map(([v, l]) => <ToggleGroupItem key={v} value={v} className="px-3 text-xs">{l}</ToggleGroupItem>)}
    </ToggleGroup>
  )
}

export function Pick({ value, options, onChange, ro, placeholder, className }: {
  value: string; options: { value: string; label: React.ReactNode }[]; onChange: (v: string) => void; ro?: boolean; placeholder?: string; className?: string
}) {
  return (
    <Select value={value} onValueChange={onChange} disabled={ro}>
      <SelectTrigger size="sm" className={cn("h-8 w-full", className)}><SelectValue placeholder={placeholder} /></SelectTrigger>
      <SelectContent>{options.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
    </Select>
  )
}

/** A step picker: every other step, with its kind icon. */
export function TargetSelect({ spec, self, value, onChange, ro }: { spec: Spec; self: string; value?: string; onChange: (to: string) => void; ro?: boolean }) {
  const opts = Object.entries(spec.nodes).filter(([id]) => id !== self).map(([id, n]) => {
    const K = KIND[n.kind]
    return { value: id, label: <span className="flex items-center gap-2"><K.icon className={cn("size-3.5", K.text)} />{titleOf(id, n)}</span> }
  })
  return <Pick value={value ?? ""} options={opts} onChange={onChange} ro={ro} placeholder="Choose step" />
}

/** name -> step rows: routes, approval outcomes, a wait's outcomes. */
export function RouteRows({ rows, spec, self, ro, onTo }: { rows: { name: string; to?: string; when?: string }[]; spec: Spec; self: string; ro: boolean; onTo: (name: string, to: string) => void }) {
  return (
    <div className="space-y-2">
      {rows.map((r) => (
        <div key={r.name} className="space-y-1">
          <div className="grid grid-cols-[112px_16px_minmax(0,1fr)] items-center gap-1.5">
            <span className="flex h-6 w-fit max-w-full items-center truncate rounded border bg-background px-1.5 font-mono text-[11.5px]">{r.name}</span>
            <ArrowRight className="size-3.5 text-faint" />
            <TargetSelect spec={spec} self={self} value={r.to} onChange={(to) => onTo(r.name, to)} ro={ro} />
          </div>
          {r.when && <p className="pl-[130px] text-xs text-muted-foreground">{r.when}</p>}
        </div>
      ))}
    </div>
  )
}

export const AddButton = ({ label, onClick }: { label: string; onClick: () => void }) => (
  <Button variant="ghost" size="xs" className="text-muted-foreground" onClick={onClick}><Plus />{label}</Button>
)
export const RemoveButton = ({ onClick, label = "Remove" }: { onClick: () => void; label?: string }) => (
  <Button variant="ghost" size="icon-sm" className="shrink-0 text-muted-foreground" onClick={onClick} aria-label={label}><X /></Button>
)

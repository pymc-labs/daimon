import { Check, Hash, Plus } from "lucide-react"
import { useState } from "react"

import { AgentAvatar } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { cn } from "@/lib/utils"
import type { Place } from "./types"

/** One choice among a few, as a large clickable card. */
export function Cards<T extends string>({ label, value, onChange, choices, disabled = [], className }: {
  label: string; value: T; onChange: (v: T) => void; choices: Record<T, string>; disabled?: T[]; className?: string
}) {
  const keys = Object.keys(choices) as T[]
  return (
    <div role="radiogroup" aria-label={label} className={cn("grid gap-2", className)} style={{ gridTemplateColumns: `repeat(${keys.length}, minmax(0, 1fr))` }}>
      {keys.map((k) => (
        <button
          key={k} type="button" role="radio" aria-checked={value === k} disabled={disabled.includes(k)} onClick={() => onChange(k)}
          className={cn(
            "flex min-h-11 items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-sm font-medium transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50 max-sm:px-2.5",
            value === k && "border-foreground/40 bg-accent",
          )}
        >
          <span className="min-w-0 break-words">{choices[k]}</span>
          {value === k && <Check className="size-4 shrink-0 text-muted-foreground" />}
        </button>
      ))}
    </div>
  )
}

/** Agents and channels to pick from, found by name. `taken` places are left out. */
export function PlacePicker({ places, taken = [], onPick, label = "Add place" }: { places: Place[]; taken?: Place[]; onPick: (p: Place) => void; label?: string }) {
  const [open, setOpen] = useState(false)
  const free = places.filter((p) => !taken.some((t) => t.scope === p.scope && t.id === p.id))
  const group = (scope: Place["scope"], heading: string) => {
    const list = free.filter((p) => p.scope === scope)
    return list.length > 0 && (
      <CommandGroup heading={heading}>
        {list.map((p) => (
          <CommandItem key={`${p.scope}|${p.id}`} value={`${p.scope}:${p.name}:${p.id}`} onSelect={() => { setOpen(false); onPick(p) }}>
            {p.scope === "agent" ? <AgentAvatar src="" name={p.name} className="size-4 rounded" /> : <Hash className="text-muted-foreground" />}
            {p.name}
          </CommandItem>
        ))}
      </CommandGroup>
    )
  }
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild><Button size="xs" variant="outline"><Plus />{label}</Button></PopoverTrigger>
      <PopoverContent className="w-64 p-0" align="end">
        <Command>
          <CommandInput placeholder="Find an agent or channel" />
          <CommandList className="max-h-64">
            {group("agent", "Agents")}
            {group("channel", "Channels")}
            {!free.length && <div className="px-3 py-6 text-center text-sm text-muted-foreground">Nothing left to add</div>}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

/** A titled block of the page; `add` is its button, top right. */
export function Section({ title, add, children }: { title: string; add?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section>
      <div className="mb-2 flex h-8 items-center justify-between gap-3"><h2 className="text-sm font-semibold">{title}</h2>{add}</div>
      {children}
    </section>
  )
}

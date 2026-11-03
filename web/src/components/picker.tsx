import { Check, Plus, X } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { cn } from "@/lib/utils"

/** `node` replaces the icon and label in a chip, e.g. a link. */
export type PickItem = { id: string; label: string; icon?: React.ReactNode; node?: React.ReactNode }

/**
 * Picked items as removable chips, then `+ Add`: a searchable list where picking an item toggles it.
 * Chips render in `value` order; ids missing from `items` are labelled with `unknown`.
 */
export function MultiPicker({ items, value, onAdd, onRemove, placeholder = "Search", unknown = "Unknown", addLabel = "Add", disabled, className }: {
  items: PickItem[]; value: string[]; onAdd: (id: string) => void; onRemove: (id: string) => void
  placeholder?: string; unknown?: string; addLabel?: string; disabled?: boolean; className?: string
}) {
  const [open, setOpen] = useState(false)
  const byId = new Map(items.map((i) => [i.id, i]))
  return (
    <div className={cn("flex min-w-0 flex-wrap items-center gap-1.5", className)}>
      {value.map((id) => {
        const item = byId.get(id)
        return (
          <span key={id} className="inline-flex h-7 max-w-full items-center gap-1.5 rounded-md border pr-1 pl-1.5 text-xs">
            {item?.node ?? <>{item?.icon}<span className="truncate">{item?.label ?? unknown}</span></>}
            <button
              type="button" aria-label={`Remove ${item?.label ?? unknown}`} disabled={disabled}
              className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground disabled:pointer-events-none" onClick={() => onRemove(id)}
            ><X className="size-3" /></button>
          </span>
        )
      })}
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button variant="ghost" size="sm" className="h-7 px-2 text-xs text-muted-foreground" disabled={disabled}><Plus className="size-3.5" />{addLabel}</Button>
        </PopoverTrigger>
        <PopoverContent className="w-64 p-0" align="start">
          <Command>
            <CommandInput placeholder={placeholder} />
            <CommandList className="max-h-64">
              <CommandEmpty>No match</CommandEmpty>
              <CommandGroup>
                {items.map((i) => {
                  const picked = value.includes(i.id)
                  return (
                    <CommandItem key={i.id} value={`${i.label} ${i.id}`} onSelect={() => (picked ? onRemove(i.id) : onAdd(i.id))}>
                      {i.icon}
                      <span className="truncate">{i.label}</span>
                      <Check className={cn("ml-auto", !picked && "invisible")} />
                    </CommandItem>
                  )
                })}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  )
}

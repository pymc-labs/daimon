import { Check, ListFilter, Search, X } from "lucide-react"
import { useEffect, useState } from "react"
import { useSearchParams } from "react-router"

import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { cn } from "@/lib/utils"

type Opt = { value: string; label: string; icon?: React.ReactNode; count?: number }

/** URL-synced param helpers: lists are comma-separated. */
export function useParam(key: string) {
  const [params, setParams] = useSearchParams()
  const value = params.get(key) ?? ""
  const set = (v: string | null) => setParams((p) => {
    if (v) p.set(key, v)
    else p.delete(key)
    return p
  }, { replace: true })
  return [value, set] as const
}

/** Keys that press a button; only when the focus is on the element itself, not something inside it. */
const onPress = (open: () => void) => (e: React.KeyboardEvent) => {
  if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open() }
}

/** Props for any element that opens something: clickable and focusable, Enter and Space open. */
export const pressProps = (open: () => void) => ({ role: "button", tabIndex: 0, onClick: open, onKeyDown: onPress(open) })

/** Props for a table row that opens something. */
export const rowProps = (open: () => void, selected = false) => ({
  tabIndex: 0, onClick: open, "data-state": selected ? "selected" : undefined, onKeyDown: onPress(open),
})

export function useListParam(key: string) {
  const [v, set] = useParam(key)
  const list = v ? v.split(",") : []
  return [list, (l: string[]) => set(l.length ? l.join(",") : null)] as const
}

export function Facet({ label, options, value, onChange, single }: {
  label: string; options: Opt[]; value: string[]; onChange: (v: string[]) => void; single?: boolean
}) {
  const [open, setOpen] = useState(false)
  const picked = options.filter((o) => value.includes(o.value))
  const toggle = (v: string) => {
    if (single) { onChange(value.includes(v) ? [] : [v]); setOpen(false) } else onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v])
  }
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          className={cn(
            "inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs font-medium hover:bg-accent data-[state=open]:bg-accent",
            picked.length ? "border-border-strong text-foreground" : "border-dashed text-muted-foreground",
          )}
        >
          {!picked.length && <ListFilter className="size-3.5" />}
          {label}
          {picked.length > 0 && (
            <span className="max-w-40 truncate border-l pl-1.5 font-normal">
              {picked.length <= 2 ? picked.map((p) => p.label).join(", ") : `${picked.length} selected`}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-auto min-w-56 max-w-80 p-0" align="start">
        <Command>
          {options.length > 7 && <CommandInput placeholder={label} />}
          <CommandList className="max-h-72">
            <CommandEmpty>No match</CommandEmpty>
            <CommandGroup>
              {options.map((o) => (
                <CommandItem key={o.value} value={`${o.label} ${o.value}`} onSelect={() => toggle(o.value)}>
                  {single ? <Check className={cn("size-4", !value.includes(o.value) && "invisible")} /> : (
                    <span className={cn("flex size-4 items-center justify-center rounded-[4px] border", value.includes(o.value) && "border-primary bg-primary text-primary-foreground")}>
                      {value.includes(o.value) && <Check className="size-3 text-current" />}
                    </span>
                  )}
                  {o.icon}
                  <span className="truncate">{o.label}</span>
                  {o.count != null && <span className="ml-auto text-xs text-faint tabular">{o.count}</span>}
                </CommandItem>
              ))}
            </CommandGroup>
            {value.length > 0 && (
              <CommandGroup>
                <CommandItem onSelect={() => { onChange([]); setOpen(false) }} className="justify-center text-muted-foreground">Clear</CommandItem>
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

export function SearchInput({ value, onChange, placeholder = "Search", className }: {
  value: string; onChange: (v: string) => void; placeholder?: string; className?: string
}) {
  const [v, setV] = useState(value)
  useEffect(() => setV(value), [value])
  useEffect(() => {
    if (v === value) return
    const t = setTimeout(() => onChange(v), 250)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [v])
  return (
    <div className={cn("relative", className)}>
      <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-faint" />
      <input
        value={v}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Escape") { if (v) setV(""); else e.currentTarget.blur() } }}
        data-search
        placeholder={placeholder}
        className="h-7 w-full rounded-md border bg-background pr-7 pl-7 text-xs outline-none placeholder:text-faint focus:border-border-strong"
      />
      {v && (
        <button onClick={() => setV("")} className="absolute top-1/2 right-1.5 -translate-y-1/2 text-faint hover:text-foreground" aria-label="Clear">
          <X className="size-3.5" />
        </button>
      )}
    </div>
  )
}

export function FilterBar({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("flex flex-wrap items-center gap-2 px-4 py-3 lg:px-6", className)}>{children}</div>
}

export function Empty({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("py-16 text-center text-sm text-muted-foreground", className)}>{children}</div>
}

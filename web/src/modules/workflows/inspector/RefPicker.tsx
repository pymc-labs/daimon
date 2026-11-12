import { Braces, ChevronsUpDown, ShieldAlert } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import type { RefGroup } from "../spec"
import { KIND } from "../kinds"

const PURE = /^\$\{\{\s*([\w.]+)\s*\}\}$/
const short = (g: RefGroup, path: string) => path === `steps.${g.id}` || path === g.id ? "" : path.replace(g.id === "inputs" || g.id === "trigger" ? `${g.id}.` : `steps.${g.id}.`, "")

/** Values from webhooks, events and tool results stay untrusted; the icon names it on hover. */
function Untrusted() {
  return (
    <Tooltip>
      <TooltipTrigger asChild><span className="flex shrink-0 text-warning" role="img" aria-label="Untrusted"><ShieldAlert className="size-3.5" /></span></TooltipTrigger>
      <TooltipContent>Untrusted</TooltipContent>
    </Tooltip>
  )
}

/** One value: a reference picked from upstream outputs, or a raw expression. */
export function RefPicker({ value, onChange, groups, ro, open: initial = false }: { value: string; onChange: (v: string) => void; groups: RefGroup[]; ro?: boolean; open?: boolean }) {
  const path = value.match(PURE)?.[1]
  const [raw, setRaw] = useState(!!value && !path)
  const [open, setOpen] = useState(initial)
  const g = path ? groups.find((x) => x.refs.some((r) => r.path === path)) : undefined
  const ref = g?.refs.find((r) => r.path === path)
  if (raw) return (
    <div className="flex min-w-0 items-center gap-1">
      <Input value={value} readOnly={ro} onChange={(e) => onChange(e.target.value)} spellCheck={false} className="h-8 min-w-0 flex-1 font-mono text-[12px]" />
      {!ro && (
        <Tooltip>
          <TooltipTrigger asChild><Button variant="secondary" size="icon-sm" onClick={() => setRaw(false)} aria-label="Pick a reference"><Braces /></Button></TooltipTrigger>
          <TooltipContent>Pick a reference</TooltipContent>
        </Tooltip>
      )}
    </div>
  )
  const K = g?.kind ? KIND[g.kind] : null
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild disabled={ro}>
        <button className={cn("flex h-8 w-full min-w-0 items-center gap-1.5 rounded-md border bg-background px-2 text-left text-sm shadow-xs hover:border-border-strong", !path && "text-muted-foreground", open && "border-ring ring-3 ring-ring/15")}>
          {path ? (
            <>
              {K && <K.icon className={cn("size-3.5 shrink-0", K.text)} />}
              <span className="min-w-0 truncate font-mono text-[12px]" title={g?.label}>{path}</span>
              {ref?.untrusted && <Untrusted />}
              <span className="ml-auto shrink-0 text-[11px] text-faint">{ref?.type ?? "unknown"}</span>
            </>
          ) : <span className="flex-1">Choose value</span>}
          <ChevronsUpDown className="size-3.5 shrink-0 text-faint" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-[360px] p-0" align="end">
        <Command>
          <CommandInput placeholder="Search outputs" className="h-9" />
          <CommandList className="max-h-80">
            <CommandEmpty>No match</CommandEmpty>
            {groups.map((grp) => {
              const GK = grp.kind ? KIND[grp.kind] : null
              return (
                <CommandGroup key={grp.id} heading={<span className="flex items-center gap-1.5">{GK && <GK.icon className={cn("size-3", GK.text)} />}{grp.label}</span>}>
                  {grp.refs.map((r) => (
                    <CommandItem key={r.path} value={`${grp.label} ${r.path}`} onSelect={() => { onChange(`\${{ ${r.path} }}`); setOpen(false) }} className={cn(r.path === path && "bg-accent")}>
                      <span className="min-w-0 truncate font-mono text-[12px]">{short(grp, r.path) || grp.id}</span>
                      {r.untrusted && <Untrusted />}
                      <span className="ml-auto text-[11px] text-faint">{r.type}</span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              )
            })}
          </CommandList>
          <div className="flex h-10 items-center border-t px-2">
            <Button variant="ghost" size="xs" className="text-muted-foreground" onClick={() => { setRaw(true); setOpen(false) }}><Braces />Expression</Button>
          </div>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

import { Check, ChevronDown } from "lucide-react"
import { useState } from "react"

import { AgentAvatar } from "@/components/common"
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import type { AgentRef } from "@/lib/types"

const chip = "inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs font-medium"

/** The agent a chat runs as. Fixed once the chat exists, or when only one agent is reachable. */
export function AgentPicker({ agents, value, onChange }: { agents: AgentRef[]; value: AgentRef; onChange?: (a: AgentRef) => void }) {
  const [open, setOpen] = useState(false)
  const face = (
    <>
      <AgentAvatar src={value.avatar} name={value.name} className="size-4 rounded" />
      {value.name}
    </>
  )
  if (!onChange || agents.length < 2) return <span className={`${chip} text-muted-foreground`}>{face}</span>
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button type="button" className={`${chip} text-muted-foreground hover:bg-accent hover:text-foreground data-[state=open]:bg-accent`}>
          {face}
          <ChevronDown className="size-3.5 text-faint" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-[22rem] p-0" align="start" side="top">
        <Command>
          <CommandInput placeholder="Find an agent" />
          <CommandList className="max-h-80">
            <CommandGroup>
              {agents.map((a) => (
                <CommandItem key={a.id} value={`${a.name} ${a.description}`} onSelect={() => { onChange(a); setOpen(false) }} className="h-auto items-start gap-2.5 py-2">
                  <AgentAvatar src={a.avatar} name={a.name} className="mt-0.5 size-6" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{a.name}</span>
                    <span className="block truncate text-xs text-muted-foreground">{a.description}</span>
                  </span>
                  <span className="mt-0.5 shrink-0 text-xs text-faint">{a.model}</span>
                  {a.id === value.id && <Check className="mt-0.5" />}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

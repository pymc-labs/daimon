import { Check, ChevronDown, UserRound } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { useDebounced } from "@/lib/hooks"
import type { Subject } from "@/modules/credits/types"
import { usePeopleFind } from "./api"
import { platformUserId } from "./lib"

/** A person of the workspace's chat, found by name or handle, or by their id or mention; chat-only people count. */
export function PersonPicker({ platform, value, onChange }: { platform: string; value: Subject | null; onChange: (s: Subject) => void }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState("")
  const term = useDebounced(text.trim(), 200)
  const found = usePeopleFind(term)
  const id = platformUserId(platform, text)
  const people = term === text.trim() ? (found.data ?? []) : []
  const pick = (s: Subject) => { onChange(s); setOpen(false) }
  const person = (p: { id: string; name: string }) => pick({ kind: "person", id: p.id, name: p.name })
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" className="w-full justify-between font-normal">
          <span className="flex min-w-0 items-center gap-2"><UserRound className="text-muted-foreground" /><span className="truncate">{value?.name ?? "Choose"}</span></span>
          <ChevronDown className="size-4 text-faint" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0" align="start">
        <Command shouldFilter={false}>
          <CommandInput placeholder="Find a person" value={text} onValueChange={setText} />
          <CommandList className="max-h-64">
            {!!people.length && (
              <CommandGroup heading="People">
                {people.map((p) => (
                  <CommandItem key={p.id} value={`person:${p.id}`} onSelect={() => person(p)}>
                    <UserRound className="text-muted-foreground" /><span className="truncate">{p.name}</span>
                    {p.handle && p.handle !== p.name && <span className="ml-auto truncate text-xs text-muted-foreground">{p.handle}</span>}
                    {value?.id === p.id && <Check className="ml-auto" />}
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {id && !people.some((p) => p.id === id) && (
              <CommandGroup>
                <CommandItem value={`id:${id}`} onSelect={() => person({ id, name: id })}>
                  <UserRound className="text-muted-foreground" /><span className="truncate">Use id {id}</span>
                </CommandItem>
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

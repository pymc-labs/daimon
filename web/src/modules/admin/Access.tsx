import { Check, Lock, Plus, X } from "lucide-react"
import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { Command, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { usePeopleSearch } from "@/lib/hooks"
import { Block, Row } from "./shared"
import type { Access as AccessState, Providers as ProvidersState, SettingsPatch } from "./types"

type Save = (patch: SettingsPatch) => void

/** Who may start turns, and how people reach the agents in chat. */
export function Access({ access, save }: { access: AccessState; save: Save }) {
  const listed = access.people
  const ids = (listed ?? []).map((p) => p.id)
  return (
    <Block title="Access">
      <Row label="Who can use the agents">
        <Select value={listed ? "chosen" : "everyone"} onValueChange={(v) => v === "everyone" ? save({ invokers: { everyone: true, people: [] } }) : undefined}>
          <SelectTrigger size="sm" className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="everyone">Everyone</SelectItem>
            <SelectItem value="chosen" disabled={!listed}>Chosen people</SelectItem>
          </SelectContent>
        </Select>
        {!listed && <AddPerson ids={ids} onAdd={(id) => save({ invokers: { everyone: false, people: [id] } })} />}
      </Row>
      {listed && (
        <div className="flex flex-wrap items-center gap-1.5 px-4 py-2.5">
          {listed.map((p) => (
            <span key={p.id} className="inline-flex items-center gap-1 rounded-md border py-0.5 pl-2 pr-1 text-sm">
              {p.name}
              <button
                type="button"
                aria-label={`Remove ${p.name}`}
                className="rounded p-0.5 text-faint hover:text-foreground disabled:opacity-40"
                disabled={listed.length === 1 && access.others === 0}
                onClick={() => save({ invokers: { everyone: false, people: ids.filter((i) => i !== p.id) } })}
              ><X className="size-3.5" /></button>
            </span>
          ))}
          <AddPerson ids={ids} onAdd={(id) => save({ invokers: { everyone: false, people: [...ids, id] } })} />
          {access.others > 0 && <span className="ml-1 text-sm text-muted-foreground">{access.others} more from chat</span>}
        </div>
      )}
      {access.dm_enabled !== null && (
        <Row label="Direct messages"><Switch aria-label="Direct messages" checked={access.dm_enabled} onCheckedChange={(v) => save({ dm_enabled: v })} /></Row>
      )}
      {access.dm_memory_read_only !== null && (
        <Row label="Read-only memory in direct messages"><Switch aria-label="Read-only memory in direct messages" checked={access.dm_memory_read_only} onCheckedChange={(v) => save({ dm_memory_read_only: v })} /></Row>
      )}
      {access.plain_text_mentions !== null && (
        <Row label="Typed mentions"><Switch aria-label="Typed mentions" checked={access.plain_text_mentions} onCheckedChange={(v) => save({ plain_text_mentions: v })} /></Row>
      )}
    </Block>
  )
}

/** A person of the workspace, found by name. */
function AddPerson({ ids, onAdd }: { ids: string[]; onAdd: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState("")
  const [term, setTerm] = useState("")
  useEffect(() => {
    const t = setTimeout(() => setTerm(text.trim()), 200)
    return () => clearTimeout(t)
  }, [text])
  const people = usePeopleSearch(term)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" aria-label="Add a person"><Plus />Add</Button>
      </PopoverTrigger>
      <PopoverContent className="w-64 p-0" align="end">
        <Command shouldFilter={false}>
          <CommandInput placeholder="Find a person" value={text} onValueChange={setText} />
          <CommandList className="max-h-64">
            <CommandGroup>
              {(people.data ?? []).map((p) => (
                <CommandItem key={p.id} value={p.id} disabled={ids.includes(p.id)} onSelect={() => { onAdd(p.id); setOpen(false) }}>
                  {p.name}
                  {ids.includes(p.id) && <Check className="ml-auto" />}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

/** Which providers this workspace may use. Anthropic is always on. */
export function Providers({ providers, save }: { providers: ProvidersState; save: Save }) {
  const toggle = (id: string, on: boolean) =>
    save({ providers: on ? [...providers.allowed, id] : providers.allowed.filter((p) => p !== id) })
  return (
    <Block title="Providers">
      <Row label="Use the default providers">
        <Switch aria-label="Use the default providers" checked={providers.followed} onCheckedChange={(v) => save({ providers: v ? "default" : providers.allowed })} />
      </Row>
      {providers.offered.map((p) => (
        <Row key={p.id} label={p.label}>
          {p.id === "anthropic"
            ? <Lock className="size-4 text-faint" aria-label="Always on" />
            : <Switch aria-label={p.label} checked={providers.allowed.includes(p.id)} disabled={providers.followed} onCheckedChange={(v) => toggle(p.id, v)} />}
        </Row>
      ))}
    </Block>
  )
}

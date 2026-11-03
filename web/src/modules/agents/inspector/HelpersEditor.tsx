import { Plus, X } from "lucide-react"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { AgentAvatar } from "@/components/common"
import { useAgents } from "../api"
import { Dot, Locked } from "../bits"
import { Section, type Props } from "./parts"

export function HelpersEditor({ agent, config, ro, update, changed }: Props) {
  const roster = useAgents().data
  if (config.helpers == null) return <Section><Locked who={agent.owner.name} /></Section>
  const helpers = config.helpers
  const have = new Set(helpers.map((h) => h.id))
  const free = roster?.filter((a) => a.id !== agent.id && !have.has(a.id)) ?? []
  return (
    <>
      <Section title={`Attached (${helpers.length})`}>
        {!helpers.length ? <div className="text-sm text-muted-foreground">None</div> : (
          <div className="-mx-2">
            {helpers.map((h) => (
              <div key={h.id} className="group flex h-8 items-center gap-2 rounded-md px-2 text-sm hover:bg-accent">
                <AgentAvatar src={roster?.find((a) => a.id === h.id)?.avatar} name={h.name} className="size-4 text-[8px]" />
                <span className="truncate">{h.name}</span>
                {changed.has(`helper:${h.id}`) && <Dot />}
                {!ro && (
                  <button onClick={() => update((c) => { c.helpers = (c.helpers ?? []).filter((x) => x.id !== h.id) }, 0)}
                    className="ml-auto rounded p-1 text-faint opacity-0 group-hover:opacity-100 hover:bg-background hover:text-foreground" aria-label={`Remove ${h.name}`}>
                    <X className="size-3.5" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>
      {!ro && (
        <Section title="Add helper">
          <Command className="rounded-lg border">
            <CommandInput placeholder="Search agents" />
            <CommandList className="max-h-80">
              <CommandEmpty>No match</CommandEmpty>
              <CommandGroup>
                {free.map((a) => (
                  <CommandItem key={a.id} value={`${a.name} ${a.id}`} onSelect={() => update((c) => { (c.helpers ??= []).push({ id: a.id, name: a.name }) }, 0)}>
                    <Plus />
                    <AgentAvatar src={a.avatar} name={a.name} className="size-4 text-[8px]" />
                    <span className="truncate">{a.name}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </Section>
      )}
    </>
  )
}

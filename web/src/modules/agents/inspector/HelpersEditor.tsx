import { Plus, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { AgentAvatar } from "@/components/common"
import { useAgents, useSetHelpers } from "../api"
import { Dot, Locked, NotSupported } from "../bits"
import { NOTHING } from "../unsupported"
import { Section, type Props } from "./parts"

export function HelpersEditor({ agent, config, ro, update, changed, unsupported = NOTHING }: Props) {
  const lost = unsupported.capabilities.has("multiagent")
  const roster = useAgents().data
  const set = useSetHelpers(agent.id)
  // A seeded agent is read-only, but an admin may change its helper list in place.
  const live = ro && agent.can_edit_helpers
  const editable = !ro || live
  if (config.helpers == null) return <Section><Locked who={agent.owner.name} /></Section>
  const helpers = config.helpers
  const have = new Set(helpers.map((h) => h.id))
  const edit = (next: { id: string; name: string }[]) =>
    live ? set.mutate(next.map((h) => h.id)) : update((c) => { c.helpers = next }, 0)
  const free = roster?.filter((a) => a.id !== agent.id && !have.has(a.id)) ?? []
  return (
    <>
      <Section title={`Attached (${helpers.length})`}>
        {!helpers.length ? <div className="text-sm text-muted-foreground">None</div> : (
          <div className="-mx-2">
            {helpers.map((h) => (
              <div key={h.id} className="group flex h-8 items-center gap-2 rounded-md px-2 text-sm hover:bg-accent">
                <AgentAvatar src={roster?.find((a) => a.id === h.id)?.avatar} name={h.name} className="size-4 text-[8px]" />
                <span className={cn("truncate", lost && "text-muted-foreground")}>{h.name}</span>
                {lost && <NotSupported />}
                {changed.has(`helper:${h.id}`) && <Dot />}
                {editable && (
                  <button onClick={() => edit(helpers.filter((x) => x.id !== h.id))} disabled={set.isPending}
                    className="ml-auto rounded p-1 text-faint opacity-0 group-hover:opacity-100 hover:bg-background hover:text-foreground" aria-label={`Remove ${h.name}`}>
                    <X className="size-3.5" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>
      {editable && (
        <Section title="Add helper">
          <Command className="rounded-lg border">
            <CommandInput placeholder="Search agents" />
            <CommandList className="max-h-80">
              <CommandEmpty>No match</CommandEmpty>
              <CommandGroup>
                {free.map((a) => (
                  <CommandItem key={a.id} value={`${a.name} ${a.id}`} disabled={set.isPending || lost} onSelect={() => edit([...helpers, { id: a.id, name: a.name }])}>
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

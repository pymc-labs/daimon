import { Plus, X } from "lucide-react"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Dot, Locked, Pill, PlaceIcon } from "../bits"
import { placeName } from "../lib"
import { Section, type Props } from "./parts"

export function PlacesEditor({ agent, config, catalog, ro, update, changed }: Props) {
  if (config.places == null) return <Section><Locked who={agent.owner.name} /></Section>
  const places = config.places
  const has = (id: string) => places.some((p) => p.id === id)
  const toggle = (id: "dm" | "web", on: boolean) => update((c) => {
    c.places = on ? [...c.places!, id === "dm" ? { id, kind: "dm", platform: "discord", name: "Direct messages" } : { id, kind: "web", platform: "web", name: "Web chat" }]
      : c.places!.filter((p) => p.id !== id)
  }, 0)
  const channels = places.filter((p) => p.kind === "channel")
  const free = catalog?.channels.filter((c) => !has(c.id)) ?? []
  return (
    <>
      <Section
        title="Channels"
        action={!ro && free.length > 0 && (
          <Select value="" onValueChange={(id) => update((c) => { c.places!.push(structuredClone(free.find((x) => x.id === id)!)) }, 0)}>
            <SelectTrigger size="sm" className="h-7 text-xs"><Plus className="size-3.5" /><SelectValue placeholder="Add channel" /></SelectTrigger>
            <SelectContent>{free.map((c) => <SelectItem key={c.id} value={c.id}>#{c.name}{c.confidential && " (confidential)"}</SelectItem>)}</SelectContent>
          </Select>
        )}
      >
        {!channels.length ? <div className="text-sm text-muted-foreground">None</div> : (
          <div className="-my-1">
            {channels.map((p) => (
              <div key={p.id} className="group flex h-9 items-center gap-2 text-sm">
                <PlaceIcon place={p} />
                <span>{placeName(p)}</span>
                {p.confidential && <Pill>Confidential</Pill>}
                {changed.has(`place:${p.id}`) && <Dot />}
                <label className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                  Default
                  <Switch checked={!!p.default} disabled={ro} onCheckedChange={(v) => update((c) => { c.places!.find((x) => x.id === p.id)!.default = v }, 0)} />
                </label>
                {!ro && (
                  <button onClick={() => update((c) => { c.places = c.places!.filter((x) => x.id !== p.id) }, 0)} className="rounded p-1 text-faint hover:bg-accent hover:text-foreground" aria-label={`Remove ${p.name}`}>
                    <X className="size-3.5" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>
      <Section title="Also">
        <div className="-my-1">
          {([["dm", "Direct messages"], ["web", "Web chat"]] as const).map(([id, label]) => (
            <label key={id} className="flex h-9 items-center gap-2 text-sm">
              <PlaceIcon place={{ kind: id, platform: id === "web" ? "web" : "discord" }} />{label}
              {changed.has(`place:${id}`) && <Dot />}
              <Switch className="ml-auto" checked={has(id)} disabled={ro} onCheckedChange={(v) => toggle(id, v)} />
            </label>
          ))}
        </div>
      </Section>
    </>
  )
}

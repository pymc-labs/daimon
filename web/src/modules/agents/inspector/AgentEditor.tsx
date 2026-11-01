import { Avatar } from "@/components/common"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Field, Section, useLocal, type Props } from "./parts"

export function AgentEditor({ agent, config, ro, update, changed }: Props) {
  const [purpose, setPurpose] = useLocal(config.purpose, (v) => update((c) => { c.purpose = v }, 500))
  return (
    <>
      <div className="flex items-center gap-3 px-5 py-5">
        <img src={config.avatar} alt="" className="size-12 rounded-lg object-cover" />
        <div className="min-w-0">
          <div className="truncate text-lg font-semibold">{config.name}</div>
          <div className="text-xs text-muted-foreground">v{agent.version}, {agent.id}</div>
        </div>
      </div>
      <Section>
        <div className="space-y-3">
          <Field label="Name"><Input value={config.name} disabled className="h-8" /></Field>
          <Field label="Description" changed={changed.has("purpose")}><Input value={purpose} disabled={ro} onChange={(e) => setPurpose(e.target.value)} className="h-8" /></Field>
          <div className="grid grid-cols-[112px_1fr] gap-3 text-sm">
            <Label className="pt-1.5 font-normal text-muted-foreground">Avatar</Label>
            <img src={config.avatar} alt="" className="size-12 rounded-lg object-cover" />
          </div>
        </div>
      </Section>
      <Section title="People">
        <div className="space-y-1">
          {[{ ...agent.owner, role: "Owner" }, ...agent.co_owners.map((p) => ({ ...p, role: "Co-owner" }))].map((p) => (
            <div key={p.id} className="flex h-8 items-center gap-2 text-sm">
              <Avatar name={p.name} />{p.name}<span className="ml-auto text-xs text-muted-foreground">{p.role}</span>
            </div>
          ))}
        </div>
      </Section>
    </>
  )
}

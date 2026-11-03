import { AgentAvatar } from "@/components/common"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Field, Section, type Props } from "./parts"
import { useLocal } from "./useLocal"

export function AgentEditor({ agent, config, ro, update, changed }: Props) {
  const [purpose, setPurpose] = useLocal(config.purpose, (v) => update((c) => { c.purpose = v }, 500))
  return (
    <>
      <div className="flex items-center gap-3 px-5 py-5">
        <AgentAvatar src={config.avatar} name={config.name} className="size-12 rounded-lg text-base" />
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
            <AgentAvatar src={config.avatar} name={config.name} className="size-12 rounded-lg text-base" />
          </div>
        </div>
      </Section>
    </>
  )
}

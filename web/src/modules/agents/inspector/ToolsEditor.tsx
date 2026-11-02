import { Mono } from "@/components/common"
import { Switch } from "@/components/ui/switch"
import { Dot } from "../bits"
import { Section, type Props } from "./parts"

export function ToolsEditor({ config, catalog, ro, update, changed }: Props) {
  return (
    <Section>
      <div className="-my-1">
        {config.tools.map((t) => (
          <label key={t.name} className="flex h-9 items-center gap-3 text-sm">
            <span>{catalog?.tools.find((x) => x.name === t.name)?.label ?? t.name}</span>
            <Mono className="text-faint">{t.name}</Mono>
            {changed.has(`tool:${t.name}`) && <Dot />}
            <Switch className="ml-auto" checked={t.enabled} disabled={ro} onCheckedChange={(v) => update((c) => { c.tools.find((x) => x.name === t.name)!.enabled = v }, 0)} />
          </label>
        ))}
      </div>
    </Section>
  )
}

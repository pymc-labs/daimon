import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { cn } from "@/lib/utils"
import { EffectTag } from "../bits"
import { EFFORT_LABEL } from "../lib"
import type { Effort } from "../types"
import { Field, Section, type Props } from "./parts"

const EFFORTS: Effort[] = ["low", "medium", "high", "xhigh", "max"]
const PROVIDERS = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google", selfhosted: "Self-hosted" }

export function ModelEditor({ agent, config, catalog, ro, update }: Props) {
  const m = config.model
  const groups = Object.entries(PROVIDERS).map(([b, label]) => ({ b, label, models: catalog?.models.filter((x) => x.backend === b) ?? [] })).filter((g) => g.models.length)
  const cur = catalog?.models.find((x) => x.id === m.id && x.backend === m.backend)
  return (
    <>
      <Section title="Model">
        <div className="space-y-4">
          {groups.map((g) => (
            <div key={g.b}>
              <div className="mb-1.5 flex items-center gap-2 text-xs text-muted-foreground">
                {g.label}{g.b !== agent.model.backend && <EffectTag effect="threads" />}
              </div>
              <div className="overflow-hidden rounded-lg border">
                {g.models.map((x) => (
                  <button
                    key={x.id}
                    disabled={ro}
                    onClick={() => update((c) => { c.model = { ...c.model, id: x.id, backend: x.backend, speed: x.fast ? c.model.speed : "standard" } }, 0)}
                    className={cn("flex h-10 w-full items-center gap-3 border-b px-3 text-left text-sm last:border-b-0 enabled:hover:bg-accent", m.id === x.id && m.backend === x.backend && "bg-accent")}
                  >
                    <span className={cn("flex size-4 items-center justify-center rounded-full border", m.id === x.id && "border-primary")}>
                      {m.id === x.id && <span className="size-2 rounded-full bg-primary" />}
                    </span>
                    <span className="font-medium">{x.label}</span>
                    <span className="ml-auto text-xs text-muted-foreground tabular">${x.in_usd} / ${x.out_usd} per M</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </Section>
      <Section title="Settings">
        <div className="space-y-3">
          {m.backend === "anthropic" && (
            <Field label="Effort">
              <ToggleGroup type="single" value={m.effort} disabled={ro} onValueChange={(v) => v && update((c) => { c.model.effort = v as Effort }, 0)} variant="segmented" size="sm">
                {EFFORTS.map((e) => <ToggleGroupItem key={e} value={e} className="px-2.5 text-xs">{EFFORT_LABEL[e]}</ToggleGroupItem>)}
              </ToggleGroup>
            </Field>
          )}
          {cur?.fast && (
            <Field label="Fast mode">
              <span className="flex items-center gap-3">
                <Switch checked={m.speed === "fast"} disabled={ro} onCheckedChange={(v) => update((c) => { c.model.speed = v ? "fast" : "standard" }, 0)} />
                <span className="text-xs text-muted-foreground">6x price</span>
              </span>
            </Field>
          )}
        </div>
      </Section>
    </>
  )
}

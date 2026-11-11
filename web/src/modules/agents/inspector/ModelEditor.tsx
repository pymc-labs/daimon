import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { EFFORT_LABEL, PROVIDER } from "../lib"
import { ModelPreviewPanel } from "../ModelPreviewPanel"
import type { Backend, CatalogModel, Effort } from "../types"
import { usePendingPreview } from "../usePendingPreview"
import { Field, Section, type Props } from "./parts"

const CLAUDE_EFFORTS: Effort[] = ["low", "medium", "high", "xhigh", "max"]

/** The efforts a model carries: every level on Claude, the profile's own elsewhere. */
const effortsOf = (x: CatalogModel | undefined, backend: Backend): Effort[] =>
  backend === "anthropic" ? CLAUDE_EFFORTS : (x?.efforts ?? [])

/**
 * The effort a model pick keeps. Unset stays unset off Claude; Claude's top levels
 * become the provider's highest; another missing level falls to the middle one.
 */
const carriedEffort = (effort: Effort | null, carried: Effort[], backend: Backend): Effort | null => {
  if (effort === null) return backend === "anthropic" ? "high" : null
  if (carried.length === 0 || carried.includes(effort)) return effort
  if (effort === "xhigh" || effort === "max") return carried.at(-1)!
  return carried.includes("medium") ? "medium" : carried[0]!
}

export function ModelEditor({ agent, config, catalog, ro, update }: Props) {
  const m = config.model
  const cur = catalog?.models.find((x) => x.id === m.id && x.backend === m.backend)
  const preview = usePendingPreview(agent, m).data
  const efforts = effortsOf(cur, m.backend)
  const noEffort = efforts.length === 0 && `${PROVIDER[m.backend]} has no effort setting`
  const noFast = !cur?.fast && (m.backend === "anthropic" ? "This model has no fast mode" : "Fast mode is Claude only")
  return (
    <>
      <Section title="Model">
        <div className="space-y-2">
          {catalog?.models.map((x) => {
            const on = m.id === x.id && m.backend === x.backend
            return (
              <button
                key={`${x.backend}:${x.id}`}
                disabled={ro}
                onClick={() => update((c) => {
                  const effort = carriedEffort(c.model.effort, effortsOf(x, x.backend), x.backend)
                  c.model = { ...c.model, id: x.id, backend: x.backend, speed: x.fast ? c.model.speed : "standard", effort }
                }, 0)}
                className={cn("flex min-h-12 w-full items-center gap-3 rounded-lg border px-3 py-2 text-left text-sm enabled:hover:bg-accent", on && "border-primary bg-accent")}
              >
                <span className={cn("flex size-4 shrink-0 items-center justify-center rounded-full border", on && "border-primary")}>
                  {on && <span className="size-2 rounded-full bg-primary" />}
                </span>
                <span className="font-medium">{x.label}</span>
                <span className="inline-flex h-5 items-center rounded bg-muted px-1.5 text-[11px] font-medium text-muted-foreground">{PROVIDER[x.backend]}</span>
                <span className="ml-auto text-xs text-muted-foreground tabular">${x.in_usd} / ${x.out_usd} per M</span>
              </button>
            )
          })}
        </div>
        {preview && <ModelPreviewPanel preview={preview} className="mt-3" />}
      </Section>
      <Section title="Settings">
        <div className="space-y-3">
          <Field label="Effort">
            <Reason reason={noEffort}>
              <ToggleGroup type="single" value={m.effort ?? ""} disabled={ro || !!noEffort} onValueChange={(v) => v && update((c) => { c.model.effort = v as Effort }, 0)} variant="segmented" size="sm">
                {(efforts.length ? efforts : CLAUDE_EFFORTS).map((e) => <ToggleGroupItem key={e} value={e} className="px-2.5 text-xs">{EFFORT_LABEL[e]}</ToggleGroupItem>)}
              </ToggleGroup>
            </Reason>
          </Field>
          <Field label="Fast mode">
            <Reason reason={noFast}>
              <span className="flex items-center gap-3">
                <Switch checked={m.speed === "fast"} disabled={ro || !!noFast} onCheckedChange={(v) => update((c) => { c.model.speed = v ? "fast" : "standard" }, 0)} />
                {!noFast && <span className="text-xs text-muted-foreground">6x price</span>}
              </span>
            </Reason>
          </Field>
        </div>
      </Section>
    </>
  )
}

/** Wraps a disabled control so hovering it says why. */
function Reason({ reason, children }: { reason: string | false; children: React.ReactNode }) {
  if (!reason) return <>{children}</>
  return (
    <Tooltip>
      <TooltipTrigger asChild><span className="inline-flex w-fit opacity-60">{children}</span></TooltipTrigger>
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  )
}

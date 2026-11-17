import { Lock } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"

import { InheritedSelect } from "@/components/inherited"
import { Button } from "@/components/ui/button"
import { absolute } from "@/lib/format"
import { useFeatureSave, useFeatures, useFeatureSchema } from "@/modules/features/api"
import type { FeatureSchema, FeatureView } from "@/modules/features/types"
import { Field, Section, type Props } from "../inspector/parts"
import { useMonitoringState } from "./useMonitoringState"

const DEFAULT = "default"
const FROM: Record<string, string> = { deployment: "Built-in", default: "Built-in", workspace: "Workspace" }
const SWITCHES = [
  { key: "monitor_direct_messages", label: "Direct messages", state: "direct_messages" },
  { key: "monitor_mcp", label: "Coding tools", state: "coding_tools" },
] as const

/** The two monitoring switches of one agent and the way to the activity they keep; shown only where `useMonitoringState` succeeds. */
export function MonitoringPanel({ agent }: Pick<Props, "agent">) {
  const place = { agent: agent.id }
  const schema = useFeatureSchema()
  const doc = useFeatures(place)
  const save = useFeatureSave(place)
  const state = useMonitoringState(agent.id)
  const [error, setError] = useState<string | null>(null)
  const views = new Map((doc.data?.features ?? []).map((f) => [f.key, f]))

  const row = (key: string, label: string, since: string | null | undefined) => {
    const s: FeatureSchema | undefined = schema.data?.find((x) => x.key === key)
    const v: FeatureView | undefined = views.get(key)
    if (!s || !v) return null
    const own = v.tiers.find((t) => t.tier === "agent")
    const ownValue = own && own.value !== DEFAULT ? own.value : null
    const label_ = (value: string) => s.values.find((o) => o.value === value)?.label ?? value
    return (
      <Field key={key} label={label}>
        {v.locked && v.source !== "agent" ? (
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 text-sm">
            <span className="inline-flex items-center gap-1.5"><Lock className="size-3.5 text-muted-foreground" />{label_(v.value)}</span>
            <span className="text-xs text-muted-foreground">Locked by the {(FROM[v.source] ?? "Workspace").toLowerCase()}</span>
          </span>
        ) : (
          <div className="min-w-0 space-y-1">
            <InheritedSelect
              bare label={label} value={ownValue} disabled={save.pending} className="w-full max-w-60"
              inherited={{ value: v.value, label: ownValue === null ? label_(v.value) : "", from: FROM[v.source] ?? "Built-in" }}
              options={s.values.map((o) => ({ value: o.value, label: o.label }))}
              onChange={async (next) => setError(await save.run({ key, tier: "agent", value: next ?? DEFAULT, locked: false, expected: own?.text ?? DEFAULT }))}
            />
            {v.value === "on" && since && <div className="text-xs text-muted-foreground">Since {absolute(since)}</div>}
          </div>
        )}
      </Field>
    )
  }

  return (
    <>
      <Section>
        <div className="space-y-3">
          {SWITCHES.map((w) => row(w.key, w.label, state.data?.[w.state].since))}
        </div>
        {error && <p role="alert" className="mt-3 text-xs text-destructive">{error}</p>}
      </Section>
      {state.isSuccess && (
        <Section>
          <div className="flex justify-end">
            <Button size="sm" asChild><Link to={`/admin/monitoring?agent=${encodeURIComponent(agent.id)}`}>View activity</Link></Button>
          </div>
        </Section>
      )}
    </>
  )
}

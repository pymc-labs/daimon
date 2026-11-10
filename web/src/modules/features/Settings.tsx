import { Lock, LockOpen } from "lucide-react"
import { useState } from "react"

import { InheritedSelect } from "@/components/inherited"
import { PanelSection, SettingRow } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { SettingsList, type SettingsRow } from "@/components/settings"
import { Button } from "@/components/ui/button"
import { useFeatureSave, useFeatures, useFeatureSchema } from "./api"
import type { FeaturePlace, FeatureSchema, FeatureView, Tier } from "./types"

const DEFAULT = "default"
const TIER_NAME: Record<string, string> = { deployment: "Built-in", default: "Built-in", workspace: "Workspace", agent: "Agent", channel: "Channel", thread: "Thread" }

type Group = { name: string; rows: { key: string; label: string; control: React.ReactNode }[] }

/** The label of a value as people read it. */
const labelOf = (s: FeatureSchema, value: string) => s.values.find((v) => v.value === value)?.label ?? value

/**
 * The controls for every feature that can be set at `tier`, grouped by category, from the schema.
 * Each control is the value set at this tier, or the inherited one with where it comes from.
 * A person who may not change it, or a lock above, sees the value in effect instead; so does
 * a value another level overrides, next to its control.
 */
function useGroups(place: FeaturePlace, tier: Tier, edit: boolean, report: (error: string | null) => void) {
  const schema = useFeatureSchema()
  const doc = useFeatures(place)
  const save = useFeatureSave(place)
  const views = new Map((doc.data?.features ?? []).map((f) => [f.key, f]))
  const run = async (s: FeatureSchema, v: FeatureView, value: string, locked: boolean) => {
    const own = v.tiers.find((t) => t.tier === tier)
    report(await save.run({ key: s.key, tier, value, locked, expected: own?.text ?? DEFAULT }))
  }

  const control = (s: FeatureSchema, v: FeatureView) => {
    const own = v.tiers.find((t) => t.tier === tier)
    const ownValue = own && own.value !== DEFAULT ? own.value : null
    if (v.unavailable) return <span className="text-muted-foreground">{v.summary}</span>
    if (!edit || (v.locked && v.source !== tier)) return <span>{v.summary}</span>
    const overridden = ownValue !== null && v.source !== tier && v.value !== ownValue
    const lockable = s.lockable.includes(tier) && ownValue !== null ? own : undefined
    return (
      <>
        <InheritedSelect
          bare label={s.name} value={ownValue} disabled={save.pending} className="w-60"
          inherited={{ value: v.value, label: ownValue === null ? labelOf(s, v.value) : "", from: TIER_NAME[v.source] ?? "Built-in" }}
          options={s.values.map((o) => ({ value: o.value, label: o.label }))}
          onChange={(next) => void run(s, v, next ?? DEFAULT, false)}
        />
        {overridden && <span className="text-xs text-muted-foreground">{v.summary}</span>}
        {lockable && (
          <Button
            variant="ghost" size="icon-sm" disabled={save.pending} aria-pressed={lockable.locked}
            aria-label={`${lockable.locked ? "Unlock" : "Lock"} ${s.name}`} title={lockable.locked ? "Locked below this level" : "Lock below this level"}
            onClick={() => void run(s, v, lockable.value, !lockable.locked)}
          >{lockable.locked ? <Lock /> : <LockOpen />}</Button>
        )}
      </>
    )
  }

  const groups: Group[] = []
  for (const s of schema.data ?? []) {
    const v = views.get(s.key)
    if (!v || !s.tiers.includes(tier) || (v.unavailable && v.tiers.every((t) => t.value === DEFAULT))) continue
    let g = groups.find((x) => x.name === s.categoryName)
    if (!g) groups.push((g = { name: s.categoryName, rows: [] }))
    g.rows.push({ key: s.key, label: s.name, control: control(s, v) })
  }
  return { groups, schema, doc }
}

type Props = { place: FeaturePlace; tier: Tier; edit: boolean }

/** A panel's Settings: one section per category, rows as `SettingRow`s. */
export function FeatureSettings({ place, tier, edit }: Props) {
  const [error, setError] = useState<string | null>(null)
  const { groups, schema, doc } = useGroups(place, tier, edit, setError)
  return (
    <QueryState query={schema.data ? doc : schema} className="px-5 py-4">{() => (
      <>
        {groups.map((g) => (
          <PanelSection key={g.name} title={g.name}>
            <div className="space-y-3">
              {g.rows.map((r) => <SettingRow key={r.key} label={r.label}><div className="flex items-center gap-2">{r.control}</div></SettingRow>)}
            </div>
          </PanelSection>
        ))}
        {error && <p role="alert" className="px-5 pb-3 text-xs text-destructive">{error}</p>}
      </>
    )}</QueryState>
  )
}

/** A page's Settings: one bordered list per category. */
export function FeatureList({ place, tier, edit }: Props) {
  const [error, setError] = useState<string | null>(null)
  const { groups, schema, doc } = useGroups(place, tier, edit, setError)
  return (
    <QueryState query={schema.data ? doc : schema} className="px-4 pt-6 lg:px-6">{() => (
      <div className="px-4 py-6 lg:px-6">
        {groups.map((g, i) => (
          <SettingsList key={g.name} title={g.name} className={i ? "mt-6" : undefined} rows={g.rows as SettingsRow[]} />
        ))}
        {error && <p role="alert" className="mx-auto mt-3 max-w-3xl text-xs text-destructive">{error}</p>}
      </div>
    )}</QueryState>
  )
}

import { Check, TriangleAlert, X } from "lucide-react"
import { type ReactNode, useState } from "react"

import { InheritedSelect } from "@/components/inherited"
import { QueryState } from "@/components/QueryState"
import { SettingsList } from "@/components/settings"
import { useConfirm } from "@/lib/confirm"
import { plural } from "@/lib/format"
import { useFeatures } from "@/modules/features/api"
import { FeatureToggle, Locked, Unavailable } from "@/modules/features/Settings"
import { usePlaces, useSupportPage, useSupportSave } from "./api"
import { Limits } from "./Limits"
import { Menus } from "./Menus"
import { PlacePicker, Section } from "./parts"
import type { Place, SupportPage } from "./types"

/** Admin > Support: Ask a person, in the order an admin sets it up. */
export function Support() {
  const q = useSupportPage()
  const features = useFeatures({})
  const on = features.data?.features.find((f) => f.key === "support_escalation")?.value !== "off"
  return (
    <QueryState query={q}>{(d) => (
      <div className="mx-auto w-full max-w-3xl space-y-8 px-4 py-6 lg:px-6">
        <Section title="Ask a person"><SettingsList className="max-w-none" rows={[{ label: "Workspace", control: <FeatureToggle place={{}} tier="workspace" name="support_escalation" /> }]} /></Section>
        {on && (
          <>
            <Where d={d} />
            <Destination d={d} />
            <Limits d={d} />
            <Menus d={d} />
          </>
        )}
      </div>
    )}</QueryState>
  )
}

const KINDS = { agent: "Agent", channel: "Channel" }

/** Step 2: after a thumbs down, and the agents and channels that set their own. */
function Where({ d }: { d: SupportPage }) {
  const places = usePlaces(d)
  // A place just added stays listed while it is still on the default.
  const [added, setAdded] = useState<Place[]>([])
  const rows = [...d.places, ...added.filter((a) => !d.places.some((p) => p.scope === a.scope && p.id === a.id))]
  return (
    <Section title="Where it shows" add={<PlacePicker places={places} taken={rows} onPick={(p) => setAdded((a) => [...a, p])} />}>
      <SettingsList
        className="max-w-none"
        rows={[
          { key: "feedback", label: "After a thumbs down", control: <FeatureToggle place={{}} tier="workspace" name="support_feedback" /> },
          ...rows.map((p) => ({
            key: `${p.scope}|${p.id}`,
            label: <span className="flex items-center gap-2">{p.name}<span className="text-xs text-muted-foreground">{KINDS[p.scope]}</span></span>,
            control: <FeatureToggle place={p.scope === "agent" ? { agent: p.id } : { channel: p.id }} tier={p.scope} name="support_escalation" />,
          })),
        ]}
      />
    </Section>
  )
}

/** Step 3: the channel requests are posted in, and whether it works. */
export function Destination({ d }: { d: SupportPage }) {
  const c = d.channel
  const save = useSupportSave()
  const confirm = useConfirm()
  const places = usePlaces(d).filter((p) => p.scope === "channel")
  const [error, setError] = useState<string | null>(null)
  const pick = async (id: string | null) => {
    if (c.open.count && !(await confirm({ title: "Change where requests go?", body: `${plural(c.open.count, "open request")} stay in ${c.open.where}. New ones go to the new channel.`, action: "Change" }))) return
    const out = await save.run("channel", { channel_id: id, expected_revision: c.revision })
    setError(out.problems.find((p) => p.loc[0] === "channel_id")?.msg ?? out.error)
  }
  const problem = (icon: ReactNode, label: string) => <span className="flex flex-wrap items-center gap-x-2"><span className="inline-flex items-center gap-1.5">{icon}{label}</span><span className="text-xs text-muted-foreground">{c.detail}</span></span>
  const state = c.state === "ready"
    ? <span className="inline-flex items-center gap-1.5"><Check className="size-3.5 text-success" />Ready</span>
    : c.state === "blocked"
      ? problem(<X className="size-3.5 text-destructive" />, "Can't post")
      : c.state === "unverified"
        ? problem(<TriangleAlert className="size-3.5 text-warning" />, "Unverified")
        : <Unavailable reason={c.detail ?? "Pick a support channel."} />
  const options = places.map((p) => ({ value: p.id, label: p.name }))
  if (c.value && !options.some((o) => o.value === c.value!.id)) options.unshift({ value: c.value.id, label: c.value.name })
  return (
    <div>
      <Section title="Requests go to"><SettingsList
        className="max-w-none"
        rows={[
          {
            key: "to", label: "Channel",
            control: !c.own_allowed
              ? <Locked label={c.default?.name ?? "None"} by="deployment" />
              : <InheritedSelect bare label="Channel" value={c.value?.id ?? null} disabled={save.pending} inherited={{ value: "", label: c.default?.name ?? "None", from: "Built-in" }} options={options} onChange={(v) => void pick(v)} />,
          },
          { key: "state", label: "State", control: state },
          { key: "open", label: "Open requests", control: <span className={c.open.count ? undefined : "text-muted-foreground"}>{c.open.count || "None"}</span> },
          ...(d.undelivered.length ? [{ key: "undelivered", label: "Not delivered", control: <span className="text-destructive">{d.undelivered.length}</span> }] : []),
        ]}
      /></Section>
      {error && <p role="alert" className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  )
}

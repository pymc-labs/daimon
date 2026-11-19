import { useEffect, useState } from "react"
import { Lock, Users } from "lucide-react"
import { MultiPicker } from "@/components/picker"
import { SettingsList, type SettingsRow } from "@/components/settings"
import { Switch } from "@/components/ui/switch"
import { usePeopleSearch } from "@/lib/hooks"
import type { Access as AccessState, Providers as ProvidersState, SettingsPatch } from "./types"

type Save = (patch: SettingsPatch) => void

const EVERYONE = "everyone"
const sw = (label: string, checked: boolean, on: (v: boolean) => void, extra?: { disabled?: boolean }) => (
  <Switch aria-label={label} checked={checked} onCheckedChange={on} {...extra} />
)

/** Who may start turns. */
export function Access({ access, save }: { access: AccessState; save: Save }) {
  const listed = access.people
  const ids = (listed ?? []).map((p) => p.id)
  const [text, setText] = useState("")
  const [term, setTerm] = useState("")
  useEffect(() => {
    const t = setTimeout(() => setTerm(text.trim()), 200)
    return () => clearTimeout(t)
  }, [text])
  const found = usePeopleSearch(term)
  const known = new Map([...(found.data ?? []), ...(listed ?? [])].map((p) => [p.id, p.name]))
  const items = [{ id: EVERYONE, label: "Everyone", icon: <Users className="size-3.5 text-muted-foreground" /> }, ...[...known].map(([id, label]) => ({ id, label }))]
  const lastOne = listed?.length === 1 && access.others === 0
  const rows: SettingsRow[] = [{
    key: "invokers", label: "Who can use the agents",
    control: (
      <>
        <MultiPicker
          items={items} value={listed ? ids : [EVERYONE]} onSearch={setText} locked={listed ? (lastOne ? ids : []) : [EVERYONE]} placeholder="Find a person" unknown="Unknown person"
          onAdd={(id) => save({ invokers: id === EVERYONE ? { everyone: true, people: [] } : { everyone: false, people: [...ids, id] } })}
          onRemove={(id) => save({ invokers: { everyone: false, people: ids.filter((i) => i !== id) } })}
        />
        {listed && access.others > 0 && <span className="text-muted-foreground">{access.others} more from chat</span>}
      </>
    ),
  }]
  return <SettingsList title="Access" rows={rows} />
}

/** Which providers this workspace may use. Anthropic is always on. */
export function Providers({ providers, save }: { providers: ProvidersState; save: Save }) {
  const toggle = (id: string, on: boolean) =>
    save({ providers: on ? [...providers.allowed, id] : providers.allowed.filter((p) => p !== id) })
  return (
    <SettingsList title="Providers" rows={[
      { key: "default", label: "Use the default providers", control: sw("Use the default providers", providers.followed, (v) => save({ providers: v ? "default" : providers.allowed })) },
      ...providers.offered.map((p) => ({
        key: p.id, label: p.label,
        control: p.id === "anthropic"
          ? <Lock className="size-4 text-faint" aria-label="Always on" />
          : sw(p.label, providers.allowed.includes(p.id), (v) => toggle(p.id, v), { disabled: providers.followed }),
      })),
    ]} />
  )
}

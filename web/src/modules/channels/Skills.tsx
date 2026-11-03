import { useState } from "react"

import { SkillLink } from "@/components/links"
import { PanelSection } from "@/components/panel"
import { MultiPicker } from "@/components/picker"
import { useCatalog } from "@/modules/agents/api"
import { useSave, type Outcome } from "./api"
import { Notice } from "./shared"
import type { ChannelDetail, ChannelSettings } from "./types"

export function Skills({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const { data } = useCatalog()
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage
  const expected = s.skills.map((k) => k.skill_id)
  if (!edit && !s.skills.length) return null

  const change = async (action: "add" | "remove", skill: string) => setOut(await save.run("post", "skills", { action, skill, expected }))
  const chip = (id: string, name: string) => <SkillLink id={id} name={name} />
  const attached = new Map(s.skills.map((k) => [k.skill_id, k.name]))
  const items = [
    ...(data?.skills ?? []).map((k) => ({ id: k.skill_id, label: k.title })),
    ...s.skills.filter((k) => !(data?.skills ?? []).some((x) => x.skill_id === k.skill_id)).map((k) => ({ id: k.skill_id, label: k.name })),
  ].map((i) => ({ ...i, node: attached.has(i.id) ? chip(i.id, i.label) : undefined }))

  return (
    <PanelSection title="Skills">
      {edit ? (
        <MultiPicker
          items={items} value={expected} disabled={save.pending} placeholder="Search skills" unknown="Deleted skill"
          onAdd={(id) => void change("add", id)} onRemove={(id) => void change("remove", id)}
        />
      ) : (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm">{s.skills.map((k) => <span key={k.skill_id}>{chip(k.skill_id, k.name)}</span>)}</div>
      )}
      <Notice out={out} />
    </PanelSection>
  )
}

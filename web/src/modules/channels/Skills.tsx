import { X } from "lucide-react"
import { useId, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { PanelSection } from "@/modules/admin/shared"
import { useCatalog } from "@/modules/agents/api"
import { useSave, type Outcome } from "./api"
import { Notice } from "./shared"
import type { ChannelDetail, ChannelSettings } from "./types"

function AddSkill({ onAdd, busy }: { onAdd: (skill: string) => Promise<boolean>; busy: boolean }) {
  const { data } = useCatalog()
  const list = useId()
  const [name, setName] = useState("")
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => { e.preventDefault(); if (name.trim()) void onAdd(name.trim()).then((ok) => ok && setName("")) }}
    >
      <Input list={list} value={name} onChange={(e) => setName(e.target.value)} placeholder="Skill name" aria-label="Skill name" className="h-8 w-full max-w-52" />
      <datalist id={list}>{(data?.skills ?? []).map((s) => <option key={s.skill_id} value={s.title} />)}</datalist>
      <Button size="sm" type="submit" disabled={busy || !name.trim()}>Add</Button>
    </form>
  )
}

export function Skills({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage
  const expected = s.skills.map((k) => k.skill_id)
  if (!edit && !s.skills.length) return null

  const change = async (action: "add" | "remove", skill: string) => {
    const r = await save.run("post", "skills", { action, skill, expected })
    setOut(r)
    return !r.error
  }
  return (
    <PanelSection title="Skills here">
      <div className="space-y-3 text-sm">
        {s.skills.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {s.skills.map((k) => (
              <span key={k.skill_id} className="inline-flex h-7 max-w-full items-center gap-1.5 rounded-md border pr-1 pl-2 text-xs">
                <span className="truncate">{k.name}</span>
                {edit && (
                  <button
                    type="button" aria-label={`Remove ${k.name}`} disabled={save.pending}
                    className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground" onClick={() => void change("remove", k.skill_id)}
                  ><X className="size-3" /></button>
                )}
              </span>
            ))}
          </div>
        )}
        {edit && <AddSkill onAdd={(n) => change("add", n)} busy={save.pending} />}
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

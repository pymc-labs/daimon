import { X } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { PanelSection } from "@/modules/admin/shared"
import { useCatalog } from "@/modules/agents/api"
import { useSave, type Outcome } from "./api"
import { Notice } from "./shared"
import type { ChannelDetail, ChannelSettings } from "./types"

function AddSkill({ onAdd, busy, attached }: { onAdd: (skill: string) => Promise<boolean>; busy: boolean; attached: string[] }) {
  const { data } = useCatalog()
  const [name, setName] = useState("")
  const have = new Set(attached)
  const choices = [...new Set((data?.skills ?? []).map((s) => s.title))].filter((t) => !have.has(t))
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => { e.preventDefault(); if (name) void onAdd(name).then((ok) => ok && setName("")) }}
    >
      <Select value={name} onValueChange={setName}>
        <SelectTrigger size="sm" className="h-8 w-full max-w-52" aria-label="Skill"><SelectValue placeholder="Choose a skill" /></SelectTrigger>
        <SelectContent>{choices.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
      </Select>
      <Button size="sm" type="submit" disabled={busy || !name}>Add</Button>
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
        {edit && <AddSkill onAdd={(n) => change("add", n)} busy={save.pending} attached={s.skills.map((k) => k.name)} />}
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

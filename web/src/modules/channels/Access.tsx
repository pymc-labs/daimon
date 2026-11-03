import { useState } from "react"

import { PanelSection, SettingRow } from "@/components/panel"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useChoose } from "@/lib/confirm"
import { useSave, type Outcome } from "./api"
import { accessAsk, accessFlags } from "./lib"
import { Notice } from "./shared"
import type { ChannelDetail, Choice, Readers, Writers } from "./types"

function Rule<T extends string>({ label, value, shown, options, edit, busy, onPick }: {
  label: string; value: T; shown: string; options: Choice<T>[]; edit: boolean; busy: boolean; onPick: (v: T) => void
}) {
  return (
    <SettingRow label={label}>
      {edit ? (
        <Select value={value} onValueChange={(v) => onPick(v as T)} disabled={busy}>
          <SelectTrigger size="sm" className="w-full max-w-72" aria-label={label}><SelectValue /></SelectTrigger>
          <SelectContent>{options.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
        </Select>
      ) : <span>{shown}</span>}
    </SettingRow>
  )
}

export function Access({ c }: { c: ChannelDetail }) {
  const save = useSave(c.id)
  const choose = useChoose()
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage

  const change = async (next: { readers?: Readers; writers?: Writers }) => {
    const ask = accessAsk(c, next)
    const pick = ask ? await choose(ask) : "action"
    if (!pick) return
    setOut(await save.run("put", "access", { ...next, expected: { readers: c.readers, writers: c.writers }, ...accessFlags(c, next, pick) }))
  }

  return (
    <PanelSection title="Access">
      <div className="space-y-3">
        <Rule label="Read" value={c.readers} shown={c.readers_label} options={c.readers_options} edit={edit} busy={save.pending} onPick={(readers) => readers !== c.readers && void change({ readers })} />
        <Rule label="Post" value={c.writers} shown={c.writers_label} options={c.writers_options} edit={edit} busy={save.pending} onPick={(writers) => writers !== c.writers && void change({ writers })} />
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

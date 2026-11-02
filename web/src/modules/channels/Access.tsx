import { useState } from "react"

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useConfirm } from "@/lib/confirm"
import { PanelSection } from "@/modules/admin/shared"
import { useSave, type Outcome } from "./api"
import { accessAsk } from "./lib"
import { Notice } from "./shared"
import type { ChannelDetail, Choice, Readers, Writers } from "./types"

function Rule<T extends string>({ label, value, shown, options, edit, busy, onPick }: {
  label: string; value: T; shown: string; options: Choice<T>[]; edit: boolean; busy: boolean; onPick: (v: T) => void
}) {
  return (
    <div className="grid grid-cols-[112px_1fr] items-center gap-3">
      <span className="text-muted-foreground">{label}</span>
      {edit ? (
        <Select value={value} onValueChange={(v) => onPick(v as T)} disabled={busy}>
          <SelectTrigger size="sm" className="w-full max-w-64" aria-label={label}><SelectValue /></SelectTrigger>
          <SelectContent>{options.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
        </Select>
      ) : <span>{shown}</span>}
    </div>
  )
}

export function Access({ c }: { c: ChannelDetail }) {
  const save = useSave(c.id)
  const confirm = useConfirm()
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage

  const change = async (next: { readers?: Readers; writers?: Writers }) => {
    const ask = accessAsk(c, next)
    if (ask && !(await confirm(ask))) return
    const toOwn = next.readers === "own" || next.writers === "own"
    const leaves = c.readers === "own" && !!next.readers && next.readers !== "own"
    setOut(await save.run("put", "access", {
      ...next, expected: { readers: c.readers, writers: c.writers }, ...(toOwn && { copy_agent: true }), ...(leaves && { release_agents: true }),
    }))
  }

  return (
    <PanelSection title="Access">
      <div className="space-y-3 text-sm">
        <Rule label="Who can read" value={c.readers} shown={c.readers_label} options={c.readers_options} edit={edit} busy={save.pending} onPick={(readers) => readers !== c.readers && void change({ readers })} />
        <Rule label="Who can post" value={c.writers} shown={c.writers_label} options={c.writers_options} edit={edit} busy={save.pending} onPick={(writers) => writers !== c.writers && void change({ writers })} />
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

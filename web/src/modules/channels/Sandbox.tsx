import { useId, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useConfirm } from "@/lib/confirm"
import { PanelSection, Tag } from "@/modules/admin/shared"
import { useSandboxNames, useSave, type Outcome } from "./api"
import { SOURCE_LABELS } from "./lib"
import { Line, Notice } from "./shared"
import type { ChannelDetail } from "./types"

export function Sandbox({ c }: { c: ChannelDetail }) {
  const save = useSave(c.id)
  const confirm = useConfirm()
  const names = useSandboxNames()
  const list = useId()
  const own = c.environment.source === "channel" ? c.environment.value : null
  const [draft, setDraft] = useState(own ?? "")
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage

  const run = async (environment: string | null) => {
    const r = await save.run("put", "environment", { environment, expected: own }, (body) =>
      confirm({ title: "Open network access?", body, action: "Continue", destructive: true }))
    if (!r.error) setDraft(environment ?? "")
    setOut(r)
  }

  return (
    <PanelSection title="Sandbox">
      <div className="space-y-3 text-sm">
        {edit ? (
          <Line label="Sandbox">
            <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (draft.trim()) void run(draft.trim()) }}>
              <Input list={list} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Workspace default" aria-label="Sandbox" className="h-8 w-full max-w-52" />
              <datalist id={list}>{names.map((n) => <option key={n} value={n} />)}</datalist>
              <Button size="sm" type="submit" disabled={save.pending || !draft.trim() || draft.trim() === own}>Save</Button>
              {own && <Button size="sm" type="button" variant="outline" disabled={save.pending} onClick={() => void run(null)}>Use default</Button>}
            </form>
          </Line>
        ) : <Line label="Sandbox"><span>{c.environment.value ?? "None"}</span></Line>}
        <Line label="Applies">
          <span className="flex flex-wrap items-center gap-1.5">
            <span>{c.environment.value ?? "None"}</span>{c.environment.source && <Tag>{SOURCE_LABELS[c.environment.source]}</Tag>}
          </span>
        </Line>
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useConfirm } from "@/lib/confirm"
import { PanelSection, Tag } from "@/modules/admin/shared"
import { useSandboxChoices, useSave, type Outcome } from "./api"
import { SOURCE_LABELS } from "./lib"
import { Line, Notice } from "./shared"
import type { ChannelDetail } from "./types"

export function Sandbox({ c }: { c: ChannelDetail }) {
  const save = useSave(c.id)
  const confirm = useConfirm()
  const names = useSandboxChoices()
  const own = c.environment.source === "channel" ? c.environment.value : null
  const [draft, setDraft] = useState(own ?? "")
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage

  const run = async (environment: string | null) => {
    const r = await save.run("put", "environment", { environment, expected: own }, (body) =>
      confirm({ title: "Allow internet access?", body, action: "Continue", destructive: true }))
    if (!r.error) setDraft(environment ?? "")
    setOut(r)
  }

  return (
    <PanelSection title="Sandbox">
      <div className="space-y-3 text-sm">
        {edit ? (
          <Line label="Set here">
            <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); if (draft.trim()) void run(draft.trim()) }}>
              <Select value={draft} onValueChange={setDraft}>
                <SelectTrigger size="sm" className="h-8 w-full max-w-52" aria-label="Sandbox"><SelectValue placeholder="Workspace default" /></SelectTrigger>
                <SelectContent>{[...new Set([...names, ...(own ? [own] : [])])].map((n) => <SelectItem key={n} value={n}>{n}</SelectItem>)}</SelectContent>
              </Select>
              <Button size="sm" type="submit" disabled={save.pending || !draft.trim() || draft.trim() === own}>Save</Button>
              {own && <Button size="sm" type="button" variant="outline" disabled={save.pending} onClick={() => void run(null)}>Use default</Button>}
            </form>
          </Line>
        ) : null}
        <Line label={edit ? "Applies" : "Sandbox"}>
          <span className="flex flex-wrap items-center gap-1.5">
            <span>{c.environment.value ?? "None"}</span>{c.environment.source && <Tag>{SOURCE_LABELS[c.environment.source]}</Tag>}
          </span>
        </Line>
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

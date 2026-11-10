import { useState } from "react"

import { InheritedSelect } from "@/components/inherited"
import { PanelSection, SettingRow } from "@/components/panel"
import { useConfirm } from "@/lib/confirm"
import { useSandboxChoices, useSave, type Outcome } from "./api"
import { Notice } from "./shared"
import type { ChannelDetail, ChannelList } from "./types"

/** The sandbox this channel runs in; a pick saves at once. */
export function Sandbox({ c, workspace }: { c: ChannelDetail; workspace?: ChannelList["default_environment"] }) {
  const save = useSave(c.id)
  const confirm = useConfirm()
  const names = useSandboxChoices()
  const [out, setOut] = useState<Outcome | null>(null)
  const own = c.environment.source === "channel" ? c.environment.value : null

  const run = async (environment: string | null) => {
    if (environment === own) return
    setOut(await save.run("put", "environment", { environment, expected: own }, (body) =>
      confirm({ title: "Allow internet access?", body, action: "Continue", destructive: true })))
  }
  return (
    <PanelSection title="Sandbox">
      {c.can_manage ? (
        <InheritedSelect
          label="Sandbox" value={own} disabled={save.pending} onChange={(v) => void run(v)}
          inherited={{ value: workspace?.value ?? "", label: workspace?.value ?? "None", from: workspace?.source === "workspace" ? "Workspace" : "Built-in" }}
          options={[...new Set([...names, ...(own ? [own] : [])])].map((n) => ({ value: n, label: n }))}
        />
      ) : (
        <SettingRow label="Sandbox">{c.environment.value ?? <span className="text-muted-foreground">None</span>}</SettingRow>
      )}
      <Notice out={out} />
    </PanelSection>
  )
}

import { useState } from "react"

import { InheritedSegmented } from "@/components/inherited"
import { PanelSection, SettingRow } from "@/components/panel"
import { useSave, type Outcome } from "./api"
import { SWITCH_SOURCE, SWITCH_VALUE, switchChange, unavailableText } from "./lib"
import { Notice } from "./shared"
import type { ChannelDetail, ChannelSettings } from "./types"

const ROWS = [
  { key: "participation", path: "participation", label: "Joins threads" },
  { key: "progress_plans", path: "progress-plans", label: "Progress plans" },
] as const

export function Switches({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage

  return (
    <PanelSection title="Behaviour">
      <div className="space-y-3">
        {ROWS.map(({ key, path, label }) => {
          const sw = s[key]
          if (!sw.available) return <SettingRow key={key} label={label}><span className="text-muted-foreground">{unavailableText(key, c.platform)}</span></SettingRow>
          if (!edit) {
            return (
              <SettingRow key={key} label={label} overridden={sw.source === "channel"}>
                <span>{SWITCH_VALUE[sw.value]}</span>{sw.source !== "channel" && <span className="ml-2 text-muted-foreground">{SWITCH_SOURCE[sw.source]}</span>}
              </SettingRow>
            )
          }
          // The inherited value is only known while this channel sets none of its own.
          const inherited = sw.own === null ? sw.value === "on" : null
          return (
            <InheritedSegmented
              key={key} label={label} disabled={save.pending}
              value={sw.own === null ? null : sw.own === "on"}
              inherited={{ value: inherited, from: sw.source === "deployment" ? "Built-in" : "Workspace" }}
              onChange={async (v) => setOut(await save.run("put", path, switchChange(v === null ? "inherit" : v ? "on" : "off", sw)))}
            />
          )
        })}
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

import { useState } from "react"

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { PanelSection, Tag } from "@/modules/admin/shared"
import { useSave, type Outcome } from "./api"
import { choiceOf, SWITCH_SOURCE, SWITCH_VALUE, switchChange, unavailableText, type SwitchChoice } from "./lib"
import { Line, Notice } from "./shared"
import type { ChannelDetail, ChannelSettings, ChannelSwitch } from "./types"

const ROWS = [
  { key: "participation", path: "participation", label: "Joins threads" },
  { key: "progress_plans", path: "progress-plans", label: "Progress plans" },
] as const

/** The value that applies and where it comes from, as two separate things. */
export const Applied = ({ s }: { s: ChannelSwitch }) => (
  <span className="flex flex-wrap items-center gap-1.5"><span>{SWITCH_VALUE[s.value]}</span><Tag>{SWITCH_SOURCE[s.source]}</Tag></span>
)

export function Switches({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage

  return (
    <PanelSection title="Threads and plans">
      <div className="space-y-3 text-sm">
        {ROWS.map(({ key, path, label }) => {
          const sw = s[key]
          if (!sw.available) return <Line key={key} label={label}><span className="text-muted-foreground">{unavailableText(key, c.platform)}</span></Line>
          const pick = async (v: string) => {
            if (!v || v === choiceOf(sw)) return
            setOut(await save.run("put", path, switchChange(v as SwitchChoice, sw)))
          }
          return (
            <div key={key} className="space-y-1.5">
              <Line label={label}>
                {edit ? (
                  <ToggleGroup type="single" variant="segmented" size="sm" value={choiceOf(sw)} onValueChange={(v) => void pick(v)} disabled={save.pending} aria-label={label}>
                    <ToggleGroupItem value="inherit" className="px-2.5 text-xs">Workspace default</ToggleGroupItem>
                    <ToggleGroupItem value="on" className="px-2.5 text-xs">On</ToggleGroupItem>
                    <ToggleGroupItem value="off" className="px-2.5 text-xs">Off</ToggleGroupItem>
                  </ToggleGroup>
                ) : <Applied s={sw} />}
              </Line>
              {edit && <Line label="Applies"><Applied s={sw} /></Line>}
            </div>
          )
        })}
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

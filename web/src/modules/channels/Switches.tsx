import { useState } from "react"

import { InheritedSelect } from "@/components/inherited"
import { PanelSection, SettingRow } from "@/components/panel"
import { useSave, type Outcome } from "./api"
import {
  APPROVAL_OPTIONS, APPROVAL_VALUE, APPROVALS_NEED_CHAT_ADMIN, approvalsChange, approvalsFrom, OVERRIDES_OFF, SETTING_NAMES, SWITCH_OPTIONS,
  SWITCH_SOURCE, SWITCH_VALUE, switchChange, switchFrom, unavailableText,
} from "./lib"
import { Notice } from "./shared"
import type { ApprovalMode, ChannelApprovals, ChannelDetail, ChannelSettings, SwitchMode } from "./types"

const ROWS = [
  { key: "participation", path: "participation" },
  { key: "progress_plans", path: "progress-plans" },
] as const

const APPROVAL_SOURCE: Record<ChannelApprovals["source"], string> = {
  default: "Built-in default", server: "Workspace default", channel: "Set here", thread: "Set in a thread",
}

/** The notes under the approvals control: overrides off, or a person who cannot change it from here. */
export function ApprovalsNotes({ a, edit }: { a: ChannelApprovals; edit: boolean }) {
  const notes = [...(a.active ? [] : [OVERRIDES_OFF]), ...(edit && !a.editable ? [APPROVALS_NEED_CHAT_ADMIN] : [])]
  return notes.length ? <p className="text-xs text-muted-foreground">{notes.join(" ")}</p> : null
}

/** The channel's Settings group: approvals, thread participation and progress plans. */
export function Switches({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const [out, setOut] = useState<Outcome | null>(null)
  const edit = c.can_manage
  const a = s.approvals

  const approvals = !a.available ? (
    <SettingRow label={SETTING_NAMES.approvals}><span className="text-muted-foreground">Not available</span></SettingRow>
  ) : edit && a.editable ? (
    <InheritedSelect<ApprovalMode>
      label={SETTING_NAMES.approvals} value={a.own} disabled={save.pending}
      inherited={{ value: a.value, label: a.own === null ? APPROVAL_VALUE[a.value] : "", from: approvalsFrom(a) }}
      options={APPROVAL_OPTIONS}
      onChange={async (v) => setOut(await save.run("put", "approvals", approvalsChange(v, a)))}
    />
  ) : (
    <SettingRow label={SETTING_NAMES.approvals} overridden={a.source === "channel"}>
      <span>{APPROVAL_VALUE[a.value]}</span>{a.source !== "channel" && <span className="ml-2 text-muted-foreground">{APPROVAL_SOURCE[a.source]}</span>}
    </SettingRow>
  )

  return (
    <PanelSection title="Settings">
      <div className="space-y-3">
        <div className="space-y-1">{approvals}{a.available && <ApprovalsNotes a={a} edit={edit} />}</div>
        {ROWS.map(({ key, path }) => {
          const sw = s[key]
          const label = SETTING_NAMES[key]
          if (!sw.available) return <SettingRow key={key} label={label}><span className="text-muted-foreground">{unavailableText(key, c.platform)}</span></SettingRow>
          if (!edit) {
            return (
              <SettingRow key={key} label={label} overridden={sw.source === "channel"}>
                <span>{SWITCH_VALUE[sw.value]}</span>{sw.source !== "channel" && <span className="ml-2 text-muted-foreground">{SWITCH_SOURCE[sw.source]}</span>}
              </SettingRow>
            )
          }
          // The inherited value is only known while this channel sets none of its own.
          return (
            <InheritedSelect<SwitchMode>
              key={key} label={label} value={sw.own} disabled={save.pending}
              inherited={{ value: sw.value, label: sw.own === null ? SWITCH_VALUE[sw.value] : "", from: switchFrom(sw) }}
              options={SWITCH_OPTIONS}
              onChange={async (v) => setOut(await save.run("put", path, switchChange(v ?? "inherit", sw)))}
            />
          )
        })}
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

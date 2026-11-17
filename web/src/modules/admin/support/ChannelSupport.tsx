import { Link } from "react-router"

import { Button } from "@/components/ui/button"
import { InheritedSelect } from "@/components/inherited"
import { PanelSection, SettingRow } from "@/components/panel"
import { useCan } from "@/lib/hooks"
import { FeatureToggle } from "@/modules/features/Settings"
import { useSupportPage, useSupportSave } from "./api"
import { Used } from "./Limits"
import { keyOf } from "./lib"

/** The channel drawer's "Ask a person": whether it shows, the channel's own limit and menu. */
export function ChannelSupport({ id, edit }: { id: string; edit: boolean }) {
  const allowed = useCan()("workspace.manage")
  const q = useSupportPage(allowed)
  const save = useSupportSave()
  const d = q.data
  if (!allowed || !d) return null
  const limit = d.limits.find((l) => l.scope === "channel" && l.scope_id === id && l.person_id === null)
  const use = d.menu_use.find((u) => u.scope === "channel" && u.scope_id === id)
  const workspace = d.menu_use.find((u) => u.scope === "workspace")
  const options = d.menus.map((m) => ({ value: m.key, label: m.name }))
  const inherited = { value: "", label: d.menus.find((m) => m.key === workspace?.menu)?.name ?? "Built-in", from: workspace?.menu ? "Workspace" : "Built-in" }
  return (
    <PanelSection title="Ask a person">
      <SettingRow label="Shows"><FeatureToggle place={{ channel: id }} tier="channel" name="support_escalation" edit={edit} /></SettingRow>
      <SettingRow label="Credits">
        <div className="flex items-center gap-3 py-1">
          {limit ? <div className="min-w-0 flex-1"><Used l={limit} /></div> : <span className="flex-1 text-muted-foreground">Not set</span>}
          {edit && <Button asChild size="xs" variant="outline"><Link to={`/admin/support?${limit ? `limit=${encodeURIComponent(keyOf(limit))}` : `add=${encodeURIComponent(id)}`}`}>{limit ? "Edit" : "Set"}</Link></Button>}
        </div>
      </SettingRow>
      {use && (
        <SettingRow label="Menu">
          <InheritedSelect
            bare label="Menu" value={use.menu} disabled={!edit || save.pending} inherited={inherited} options={options}
            onChange={(menu) => void save.run("menus/use", { scope: "channel", scope_id: id, menu, expected: use.menu })}
          />
        </SettingRow>
      )}
    </PanelSection>
  )
}

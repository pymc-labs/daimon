import { useState } from "react"
import { Link } from "react-router"

import { InheritedSelect } from "@/components/inherited"
import { QueryState } from "@/components/QueryState"
import { SettingsList } from "@/components/settings"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useConfirm } from "@/lib/confirm"
import { plural } from "@/lib/format"
import { useAgents } from "@/modules/agents/api"
import { useChannelDefaults, useChannels, useDefaultsSave, useSandboxChoices, type DefaultsKey, type Outcome } from "./api"
import {
  agentChoices, agentName, APPROVAL_OPTIONS, APPROVAL_VALUE, approvalsChange, overrideCount, SETTING_NAMES, SWITCH_OPTIONS, SWITCH_VALUE, switchChange,
  unavailableText,
} from "./lib"
import { Notice } from "./shared"
import { ApprovalsNotes } from "./Switches"
import type { ApprovalMode, ChannelDefaults, ChannelSwitch, SwitchMode } from "./types"

/** An empty slot keeps the controls above each other when nothing overrides. */
const Overrides = ({ what, n }: { what: string; n: number }) => n > 0 ? (
  <Link to={`/channels?override=${what}`} className="w-28 shrink-0 text-right text-xs whitespace-nowrap text-muted-foreground hover:text-foreground hover:underline">
    Set in {plural(n, "channel")}
  </Link>
) : <span className="w-28 shrink-0" />

/** What the workspace's channels do unless one says otherwise. */
export function DefaultsTab() {
  const query = useChannelDefaults()
  const { data: list } = useChannels()
  const { data: roster = [] } = useAgents()
  const sandboxes = useSandboxChoices()
  const save = useDefaultsSave()
  const confirm = useConfirm()
  const [out, setOut] = useState<Outcome | null>(null)
  const run = (key: DefaultsKey, body: unknown) => void save.run(key, body, (message) =>
    confirm({ title: "Allow internet access?", body: message, action: "Continue", destructive: true })).then(setOut)

  return (
    <QueryState query={query} className="px-4 pt-6 lg:px-6">{(d: ChannelDefaults) => {
      const channels = list?.channels ?? []
      const ownAgent = d.agent.source === "workspace" ? d.agent.value : null
      const ownEnv = d.environment.source === "workspace" ? d.environment.value : null
      const choices = agentChoices(roster, d.agent.value ? [d.agent.value] : [], list?.agents)
      // The built-in value is only known while the workspace has none of its own.
      const agentFrom = { value: d.agent.value ?? "", label: ownAgent ? "" : d.agent.value ? agentName(list?.agents, d.agent.value) : "None", from: "Built-in" as const }
      const envFrom = { value: d.environment.value ?? "", label: ownEnv ? "" : (d.environment.value ?? "None"), from: "Built-in" as const }
      const sw = (what: "participation" | "progress_plans", key: DefaultsKey, s: ChannelSwitch) => !s.available ? (
        <span className="text-muted-foreground">{unavailableText(what, d.platform)}</span>
      ) : (
        <InheritedSelect<SwitchMode>
          bare label={SETTING_NAMES[what]} value={s.own} disabled={save.pending} className="w-60"
          inherited={{ value: s.value, label: s.own === null ? SWITCH_VALUE[s.value] : "", from: "Built-in" }}
          options={SWITCH_OPTIONS}
          onChange={(v) => run(key, switchChange(v ?? "inherit", s))}
        />
      )
      const a = d.approvals
      const approvals = !a.available ? <span className="text-muted-foreground">Not available</span> : (
        <div className="flex flex-col items-end gap-1">
          {a.editable ? (
            <InheritedSelect<ApprovalMode>
              bare label={SETTING_NAMES.approvals} value={a.own} disabled={save.pending} className="w-60"
              inherited={{ value: a.value, label: a.own === null ? APPROVAL_VALUE[a.value] : "", from: "Built-in" }}
              options={APPROVAL_OPTIONS}
              onChange={(v) => run("approvals", approvalsChange(v, a))}
            />
          ) : <span>{APPROVAL_VALUE[a.value]}</span>}
          <ApprovalsNotes a={a} edit />
        </div>
      )
      const footer = d.cost_footer
      const costFooter = (
        <ToggleGroup
          type="single" variant="segmented" size="sm" value={footer.shown ? "shown" : "hidden"} disabled={save.pending || !footer.editable}
          aria-label={SETTING_NAMES.cost_footer}
          onValueChange={(v) => { if (v && v !== (footer.shown ? "shown" : "hidden")) run("cost-footer", { shown: v === "shown", expected: footer.shown }) }}
        >
          <ToggleGroupItem value="shown" className="px-2.5 text-xs">Shown</ToggleGroupItem>
          <ToggleGroupItem value="hidden" className="px-2.5 text-xs">Hidden</ToggleGroupItem>
        </ToggleGroup>
      )
      return (
        <div className="px-4 pt-6 pb-6 lg:px-6">
          <SettingsList title="Channels" rows={[
            {
              key: "agent", label: "Default agent",
              control: (
                <>
                  <InheritedSelect
                    bare label="Default agent" value={ownAgent} disabled={save.pending} className="w-60"
                    inherited={agentFrom}
                    options={choices.map((a) => ({ value: a.key, label: a.name }))}
                    onChange={(v) => run("agent", { agent: v, expected: ownAgent })}
                  />
                  <Overrides what="agent" n={overrideCount(channels, "agent")} />
                </>
              ),
            },
            {
              key: "sandbox", label: "Sandbox",
              control: (
                <>
                  <InheritedSelect
                    bare label="Sandbox" value={ownEnv} disabled={save.pending} className="w-60"
                    inherited={envFrom}
                    options={[...new Set([...sandboxes, ...(ownEnv ? [ownEnv] : [])])].map((n) => ({ value: n, label: n }))}
                    onChange={(v) => run("environment", { environment: v, expected: ownEnv })}
                  />
                  <Overrides what="sandbox" n={overrideCount(channels, "sandbox")} />
                </>
              ),
            },
          ]} />
          <SettingsList title="Settings" className="mt-6" rows={[
            { key: "approvals", label: SETTING_NAMES.approvals, control: approvals },
            { key: "participation", label: SETTING_NAMES.participation, control: sw("participation", "participation", d.participation) },
            { key: "progress", label: SETTING_NAMES.progress_plans, control: sw("progress_plans", "progress-plans", d.progress_plans) },
            { key: "cost_footer", label: SETTING_NAMES.cost_footer, control: costFooter },
          ]} />
          <div className="mx-auto max-w-3xl"><Notice out={out} /></div>
        </div>
      )
    }}</QueryState>
  )
}

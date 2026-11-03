import { useState } from "react"
import { Link } from "react-router"

import { InheritedSegmented, InheritedSelect } from "@/components/inherited"
import { QueryState } from "@/components/QueryState"
import { SettingsList } from "@/components/settings"
import { useConfirm } from "@/lib/confirm"
import { plural } from "@/lib/format"
import { useAgents } from "@/modules/agents/api"
import { useChannelDefaults, useChannels, useDefaultsSave, useSandboxChoices, type DefaultsKey, type Outcome } from "./api"
import { agentChoices, agentName, overrideCount, switchChange, unavailableText } from "./lib"
import { Notice } from "./shared"
import type { ChannelDefaults, ChannelSwitch } from "./types"

const Overrides = ({ what, n }: { what: string; n: number }) => (
  <Link to={`/channels?override=${what}`} className="w-28 shrink-0 text-right text-xs whitespace-nowrap text-muted-foreground hover:text-foreground hover:underline">
    {n ? `Set in ${plural(n, "channel")}` : "Not overridden"}
  </Link>
)

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
        <InheritedSegmented
          bare label={what === "participation" ? "Joins threads" : "Progress plans"} disabled={save.pending}
          value={s.own === null ? null : s.own === "on"} inherited={{ value: s.own === null ? s.value === "on" : null, from: "Built-in" }}
          onChange={(v) => run(key, switchChange(v === null ? "inherit" : v ? "on" : "off", s))}
        />
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
            { key: "participation", label: "Joins threads", control: sw("participation", "participation", d.participation) },
            { key: "progress", label: "Progress plans", control: sw("progress_plans", "progress-plans", d.progress_plans) },
          ]} />
          <div className="mx-auto max-w-3xl"><Notice out={out} /></div>
        </div>
      )
    }}</QueryState>
  )
}

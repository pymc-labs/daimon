import { useState } from "react"

import { AgentAvatar } from "@/components/common"
import { InheritedSelect } from "@/components/inherited"
import { AgentLink } from "@/components/links"
import { PanelMenu, PanelSection, SettingRow } from "@/components/panel"
import { MultiPicker } from "@/components/picker"
import { useConfirm } from "@/lib/confirm"
import { useAgents } from "@/modules/agents/api"
import { useSave, type Outcome } from "./api"
import { agentChoices, agentName, agentOf } from "./lib"
import { Notice } from "./shared"
import type { ChannelDetail, ChannelList, ChannelSettings } from "./types"

const face = (name: string, src: string | null | undefined) => <AgentAvatar src={src} name={name} className="size-4 text-[8px]" />

export function Answers({ c, workspace, settings }: { c: ChannelDetail; workspace?: ChannelList["default_agent"]; settings?: ChannelSettings }) {
  const save = useSave(c.id)
  const confirm = useConfirm()
  const [out, setOut] = useState<Outcome | null>(null)
  const { data: roster = [] } = useAgents()
  const edit = c.can_manage && c.platform !== "web"
  const own = c.agent.source === "channel" ? c.agent.value : null
  const choices = agentChoices(roster, [...new Set([...(own ? [own] : []), ...c.kept_agents])], c.agents)
  const copy = settings?.channel_copy ?? null

  const pick = async (agent: string | null) => {
    if (agent === own) return
    setOut(await save.run("put", "default-agent", { agent, expected: own }))
  }
  const keep = async (action: "add" | "remove", agent: string) =>
    setOut(await save.run("post", "kept-agents", { action, agent, expected: c.kept_agents }))
  const retire = async () => {
    if (!copy || !(await confirm({ title: "Retire the channel copy?", body: copy.name, action: "Retire", destructive: true }))) return
    setOut(await save.run("post", "channel-copy/retire", { name: copy.name, agent_id: copy.agent_id }))
  }
  if (!edit && !c.kept_agents.length && !copy) return null

  const inherited = {
    value: workspace?.value ?? "",
    label: workspace?.value ? agentName(c.agents, workspace.value) : "None",
    from: workspace?.source === "workspace" ? "Workspace" as const : "Built-in" as const,
  }
  const link = (k: string) => { const r = agentOf(c.agents, k)!; return <AgentLink id={r.id} name={r.name} avatar={r.avatar} /> }
  const items = choices.map((a) => ({ id: a.key, label: a.name, icon: face(a.name, a.avatar), node: c.kept_agents.includes(a.key) ? link(a.key) : undefined }))

  return (
    <PanelSection title="Answers">
      <div className="space-y-3">
        {edit && (
          <InheritedSelect
            label="Default agent" value={own} inherited={inherited} disabled={save.pending} onChange={(v) => void pick(v)}
            options={items.map((i) => ({ value: i.id, label: i.label, icon: i.icon }))}
          />
        )}
        {(c.kept_agents.length > 0 || edit) && (
          <SettingRow label="Kept here" top>
            {edit ? (
              <MultiPicker
                items={items} value={c.kept_agents} disabled={save.pending}
                unknown="Deleted agent" placeholder="Search agents" onAdd={(k) => void keep("add", k)} onRemove={(k) => void keep("remove", k)}
              />
            ) : (
              <span className="flex flex-wrap gap-x-3 gap-y-1">{c.kept_agents.map((k) => <span key={k}>{link(k)}</span>)}</span>
            )}
          </SettingRow>
        )}
        {copy && (
          <SettingRow label="Channel copy">
            <span className="flex min-w-0 items-center gap-1">
              <span className="truncate">{copy.name}</span>
              {edit && <PanelMenu items={[{ label: "Retire", destructive: true, disabled: save.pending, onSelect: () => void retire() }]} />}
            </span>
          </SettingRow>
        )}
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

import { X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useCan, useKey } from "@/lib/hooks"
import { nodeEffect } from "../api"
import { EffectTag, Pill } from "../bits"
import { PANEL_W } from "../Canvas"
import { History } from "../History"
import type { Props } from "./parts"
import { AgentEditor } from "./AgentEditor"
import { InstructionsEditor } from "./InstructionsEditor"
import { ModelEditor } from "./ModelEditor"
import { SkillEditor, SkillsEditor } from "./SkillEditors"
import { ToolsEditor } from "./ToolsEditor"
import { NewServer, ServerEditor } from "./ServerEditors"
import { PlacesEditor } from "./PlacesEditor"
import { HelpersEditor } from "./HelpersEditor"
import { CodingPanel } from "../setup/CodingPanel"
import { KnowledgePanel } from "../setup/KnowledgePanel"
import { GoogleBadge, ToolRulesPanel } from "../setup/ToolRulesPanel"
import { GithubPanel } from "../setup/GithubPanel"
import { KeysPanel } from "../setup/KeysPanel"
import { PeoplePanel } from "../setup/PeoplePanel"
import { nodeUi } from "../nodes/registry"

const TITLES: Record<string, string> = {
  agent: "Agent", instructions: "Instructions", model: "Model", skills: "Skills", tools: "Tools", places: "Channels", helpers: "Helpers",
  history: "History", "mcp:new": "New MCP server", keys: "Keys", coding: "Access via MCP", people: "People", knowledge: "Knowledge", github: "GitHub",
}

export function Inspector(p: Props) {
  const { node, agent, config, select } = p
  const can = useCan()
  useKey((e) => { if (e.key === "Escape") select("") }, [select])
  const kind = node.split(/[:/]/)[0]!
  const server = kind === "mcp" ? config.mcp_servers.find((s) => node.startsWith(`mcp:${s.key}`)) : undefined
  const skill = kind === "skill" ? config.skills.find((s) => `skill:${s.skill_id}` === node) : undefined
  const platform = kind === "node" ? agent.nodes.find((n) => `node:${n.kind}` === node && n.server) : undefined
  const title = server ? "MCP server" : skill ? "Skill" : platform ? platform.label : kind === "place" ? TITLES.places! : kind === "helper" ? TITLES.helpers! : TITLES[node] ?? ""
  const effect = ["history", "mcp:new", "keys", "coding", "people", "knowledge", "github"].includes(node) || (p.ro && kind !== "node") ? null : nodeEffect(kind === "place" ? "places" : kind === "helper" ? "helpers" : kind, agent.model.backend)
  let body: React.ReactNode = null
  if (node === "agent") body = <AgentEditor {...p} />
  else if (node === "instructions") body = <InstructionsEditor {...p} />
  else if (node === "model") body = <ModelEditor {...p} />
  else if (node === "skills") body = <SkillsEditor {...p} />
  else if (skill) body = <SkillEditor {...p} skill={skill} />
  else if (node === "tools") body = <><ToolsEditor {...p} /><GoogleBadge agent={agent} />{can("workspace.manage") && <ToolRulesPanel {...p} />}</>
  else if (node === "people") body = <PeoplePanel {...p} />
  else if (node === "keys") body = <KeysPanel {...p} />
  else if (node === "coding") body = <CodingPanel {...p} />
  else if (node === "knowledge") body = <KnowledgePanel {...p} />
  else if (node === "github") body = <GithubPanel {...p} />
  else if (node === "places" || kind === "place") body = <PlacesEditor {...p} />
  else if (node === "helpers" || kind === "helper") body = <HelpersEditor {...p} />
  else if (node === "mcp:new") body = <NewServer {...p} />
  else if (server) body = <ServerEditor {...p} server={server} />
  else if (node === "history") body = <History agent={agent} />
  else if (platform) {
    const { Panel } = nodeUi(platform.kind)
    body = <Panel agent={agent} node={platform} ro={p.ro} select={select} />
  }
  if (!body) return null
  return (
    <aside style={{ width: PANEL_W }} className="absolute inset-y-0 right-0 z-20 flex max-w-full flex-col border-l bg-popover shadow-[-12px_0_32px_rgb(12_31_64/0.06)]">
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        <span className="pl-1 text-xs font-medium text-muted-foreground">{title}</span>
        {effect && <EffectTag effect={effect} />}
        {p.ro && !["history", "keys", "coding", "people", "knowledge", "github"].includes(node) && kind !== "node" && <Pill>View only</Pill>}
        <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label="Close" onClick={() => select("")}><X /></Button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">{body}</div>
    </aside>
  )
}

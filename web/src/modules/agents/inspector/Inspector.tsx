import { X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useKey, useMe } from "@/lib/hooks"
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
import { AppView } from "./AppView"

const TITLES: Record<string, string> = {
  agent: "Agent", instructions: "Instructions", model: "Model", skills: "Skills", tools: "Built-in tools", places: "Where it runs",
  history: "History", "mcp:new": "New MCP server",
}

export function Inspector(p: Props) {
  const { node, agent, config, select } = p
  useKey((e) => { if (e.key === "Escape") select("") }, [select])
  const kind = node.split(/[:/]/)[0]!
  const server = kind === "mcp" ? config.mcp_servers.find((s) => node.startsWith(`mcp:${s.key}`)) : undefined
  const skill = kind === "skill" ? config.skills.find((s) => `skill:${s.skill_id}` === node) : undefined
  const app = kind === "app" ? node.slice(4) : undefined
  const appName = useMe().data?.apps.find((a) => a.key === app)?.name
  const title = server ? "MCP server" : skill ? "Skill" : app ? appName ?? "App" : kind === "place" ? TITLES.places! : TITLES[node] ?? ""
  const effect = ["history", "mcp:new"].includes(node) || app || p.ro ? null : nodeEffect(kind === "place" ? "places" : kind, agent.model.backend)
  let body: React.ReactNode = null
  if (node === "agent") body = <AgentEditor {...p} />
  else if (node === "instructions") body = <InstructionsEditor {...p} />
  else if (node === "model") body = <ModelEditor {...p} />
  else if (node === "skills") body = <SkillsEditor {...p} />
  else if (skill) body = <SkillEditor {...p} skill={skill} />
  else if (node === "tools") body = <ToolsEditor {...p} />
  else if (node === "places" || kind === "place") body = <PlacesEditor {...p} />
  else if (app) body = <AppView agent={agent} appKey={app} />
  else if (node === "mcp:new") body = <NewServer {...p} />
  else if (server) body = <ServerEditor {...p} server={server} />
  else if (node === "history") body = <History agent={agent} />
  if (!body) return null
  return (
    <aside style={{ width: PANEL_W }} className="absolute inset-y-0 right-0 z-20 flex max-w-full flex-col border-l bg-popover shadow-[-12px_0_32px_rgb(12_31_64/0.06)]">
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        <span className="pl-1 text-xs font-medium text-muted-foreground">{title}</span>
        {effect && <EffectTag effect={effect} />}
        {p.ro && node !== "history" && !app && <Pill>View only</Pill>}
        <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label="Close" onClick={() => select("")}><X /></Button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">{body}</div>
    </aside>
  )
}

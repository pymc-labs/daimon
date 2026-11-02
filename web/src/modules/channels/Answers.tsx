import { Plus, X } from "lucide-react"
import { useState } from "react"

import { AgentAvatar } from "@/components/common"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useAgents } from "@/modules/agents/api"
import { PanelSection } from "@/modules/admin/shared"
import { useSave, type Outcome } from "./api"
import { sourcedLabel } from "./lib"
import { Notice } from "./shared"
import type { ChannelDetail, Sourced } from "./types"

const WORKSPACE = "__workspace"

export function Answers({ c, workspace }: { c: ChannelDetail; workspace?: Sourced }) {
  const save = useSave(c.id)
  const [out, setOut] = useState<Outcome | null>(null)
  const { data: agents = [] } = useAgents()
  const edit = c.can_manage && c.platform !== "web"
  const own = c.agent.source === "channel" ? c.agent.value : null
  const names = agents.map((a) => a.name)
  const withCurrent = (list: string[], extra: string | null) => (extra && !list.includes(extra) ? [extra, ...list] : list)
  const avatar = (n: string) => agents.find((a) => a.name === n)?.avatar
  const base = workspace ? sourcedLabel(workspace) : "Workspace default"

  const pick = async (v: string) => {
    const agent = v === WORKSPACE ? null : v
    if (agent === own) return
    setOut(await save.run("put", "default-agent", { agent, expected: own }))
  }
  const keep = async (action: "add" | "remove", agent: string) =>
    setOut(await save.run("post", "kept-agents", { action, agent, expected: c.kept_agents }))
  const free = names.filter((n) => !c.kept_agents.includes(n))

  return (
    <PanelSection title="Who answers">
      <div className="space-y-3 text-sm">
        <div className="grid grid-cols-[112px_1fr] items-center gap-3">
          <span className="text-muted-foreground">Default agent</span>
          {edit ? (
            <Select value={own ?? WORKSPACE} onValueChange={(v) => void pick(v)} disabled={save.pending}>
              <SelectTrigger size="sm" className="w-full max-w-64" aria-label="Default agent"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value={WORKSPACE}>{base}</SelectItem>
                {withCurrent(names, own).map((n) => <SelectItem key={n} value={n}>{n}</SelectItem>)}
              </SelectContent>
            </Select>
          ) : <span className="min-w-0 truncate">{sourcedLabel(c.agent)}</span>}
        </div>
        {(c.kept_agents.length > 0 || edit) && (
          <div className="grid grid-cols-[112px_1fr] items-start gap-3">
            <span className="pt-1 text-muted-foreground">Kept here</span>
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              {c.kept_agents.map((n) => (
                <span key={n} className="inline-flex h-7 max-w-full items-center gap-1.5 rounded-md border pr-1 pl-1.5 text-xs">
                  <AgentAvatar src={avatar(n)} name={n} className="size-4 text-[8px]" />
                  <span className="truncate">{n}</span>
                  {edit && (
                    <button
                      type="button" aria-label={`Remove ${n}`} disabled={save.pending}
                      className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground" onClick={() => void keep("remove", n)}
                    ><X className="size-3" /></button>
                  )}
                </span>
              ))}
              {edit && free.length > 0 && (
                <Select value="" onValueChange={(v) => void keep("add", v)} disabled={save.pending}>
                  <SelectTrigger size="sm" className="w-auto" aria-label="Keep an agent here"><Plus className="size-3.5" />Add</SelectTrigger>
                  <SelectContent>{free.map((n) => <SelectItem key={n} value={n}>{n}</SelectItem>)}</SelectContent>
                </Select>
              )}
              {!c.kept_agents.length && !edit && <span className="text-faint">None</span>}
            </div>
          </div>
        )}
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}


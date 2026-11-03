import { useState } from "react"

import { AgentLink } from "@/components/links"
import { PanelSection } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useAgent, useAgents } from "@/modules/agents/api"
import { useSave, type Outcome } from "./api"
import { agentChoices, agentOf, RULES, SCOPES, toolName, toolRuleChange } from "./lib"
import { Notice, Rows } from "./shared"
import type { ChannelDetail, ChannelSettings, ToolRule } from "./types"

type Rule = NonNullable<ToolRule["rule"]>
type Scope = ToolRule["scope"]

function Pick<T extends string>({ value, options, onChange, label, disabled, className }: {
  value: T | ""; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string; disabled?: boolean; className?: string
}) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as T)} disabled={disabled}>
      <SelectTrigger size="sm" className={className ?? "w-full"} aria-label={label}><SelectValue placeholder={label} /></SelectTrigger>
      <SelectContent>{options.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent>
    </Select>
  )
}

const RULE_OPTIONS = Object.entries(RULES).map(([value, label]) => ({ value: value as Rule, label }))
const SCOPE_OPTIONS = Object.entries(SCOPES).map(([value, label]) => ({ value: value as Scope, label }))
const BLANK = { agent: "", tool: "", rule: "" as Rule | "", scope: "chat" as Scope }

export function ToolRules({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const { data: roster = [] } = useAgents()
  const [out, setOut] = useState<Outcome | null>(null)
  const [draft, setDraft] = useState(BLANK)
  const { editable, rules } = s.tool_rules
  const choices = agentChoices(roster, [...new Set(rules.map((r) => r.agent))], s.agents)
  const picked = useAgent(agentOf(s.agents, draft.agent)?.id ?? roster.find((a) => a.name === draft.agent)?.id ?? "")
  const toolOptions = (picked.data?.mcp_servers ?? []).flatMap((m) => m.tools.map((t) => ({ value: `${m.key}/${t.name}`, label: `${m.name}: ${t.name}` })))
  const edit = c.can_manage && editable
  if (!c.can_manage && !rules.length) return null

  const apply = async (next: Pick<ToolRule, "agent" | "tool"> & { rule: ToolRule["rule"]; scope?: Scope }) => {
    const r = await save.run("put", "tool-rules", toolRuleChange(next, rules))
    setOut(r)
    return !r.error
  }
  const ready = draft.agent && draft.tool && draft.rule

  return (
    <PanelSection title="Tool rules">
      <div className="space-y-3 text-sm">
        {rules.length > 0 && (
          <Rows>
            {rules.map((r) => {
              const who = agentOf(s.agents, r.agent)!
              return (
                <li key={`${r.agent}/${r.tool}`} className="grid grid-cols-[1fr_auto] items-center gap-x-3 gap-y-1.5 px-3 py-2 max-sm:grid-cols-1">
                  <div className="min-w-0">
                    <div className="truncate font-medium">{toolName(r)}</div>
                    <div className="text-xs text-muted-foreground"><AgentLink id={who.id} name={who.name} avatar={who.avatar} /></div>
                  </div>
                  {edit ? (
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Pick value={r.rule ?? ""} options={RULE_OPTIONS} label="Rule" disabled={save.pending} className="w-24" onChange={(rule) => void apply({ ...r, rule })} />
                      <Pick value={r.scope} options={SCOPE_OPTIONS} label="Applies to" disabled={save.pending} className="w-44" onChange={(scope) => void apply({ ...r, scope })} />
                      <Button size="xs" variant="ghost" disabled={save.pending} onClick={() => void apply({ ...r, rule: null })}>Clear</Button>
                    </div>
                  ) : <span className="text-muted-foreground">{r.rule ? RULES[r.rule] : "No rule"}, {SCOPES[r.scope].toLowerCase()}</span>}
                </li>
              )
            })}
          </Rows>
        )}
        {!rules.length && !edit && <span className="text-faint">None</span>}
        {c.can_manage && !editable && <div className="text-xs text-muted-foreground">Read only</div>}
        {edit && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-[1fr_1fr_auto_auto]">
            <Pick value={draft.agent} options={choices.map((a) => ({ value: a.key, label: a.name }))} label="Agent" onChange={(agent) => setDraft({ ...draft, agent, tool: "" })} />
            <Pick value={draft.tool} options={toolOptions} label="Tool" disabled={!draft.agent} onChange={(tool) => setDraft({ ...draft, tool })} />
            <Pick value={draft.rule} options={RULE_OPTIONS} label="Rule" onChange={(rule) => setDraft({ ...draft, rule })} className="w-full sm:w-24" />
            <Button size="sm" disabled={save.pending || !ready}
              onClick={() => void apply({ agent: draft.agent, tool: draft.tool, rule: draft.rule as Rule, scope: draft.scope }).then((ok) => ok && setDraft(BLANK))}>Add</Button>
          </div>
        )}
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}

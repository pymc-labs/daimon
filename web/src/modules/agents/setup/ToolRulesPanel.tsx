import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Mono } from "@/components/common"
import { Pill } from "../bits"
import { Section, type Props } from "../inspector/parts"
import type { AgentToolRule } from "../types"
import { useAgentToolRules, useGoogleBinding, useSetAgentToolRule } from "./api"

type Rule = NonNullable<AgentToolRule["rule"]>
const RULES: Record<Rule, string> = { allow: "Allow", ask: "Ask", deny: "Deny" }
const SCOPES: Record<AgentToolRule["scope"], string> = { chat: "Chat", chat_and_routines: "Chat and routines" }
const DAIMON = "daimon-mcp"

function Pick<T extends string>({ value, options, label, onChange, disabled, className }: {
  value: T | ""; options: [T, string][]; label: string; onChange: (v: T) => void; disabled?: boolean; className?: string
}) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as T)} disabled={disabled}>
      <SelectTrigger size="sm" className={className ?? "w-full"} aria-label={label}><SelectValue placeholder={label} /></SelectTrigger>
      <SelectContent>{options.map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent>
    </Select>
  )
}

/** Rules that hold in every channel, with Daimon's own tools among the choices. Admins only. */
export function ToolRulesPanel({ agent, config }: Props) {
  const data = useAgentToolRules(agent.id, true)
  const set = useSetAgentToolRule(agent.id)
  const [draft, setDraft] = useState<{ tool: string; rule: Rule | ""; scope: AgentToolRule["scope"] }>({ tool: "", rule: "", scope: "chat" })
  const active = data.data?.active === true
  const tools: [string, string][] = config.mcp_servers.flatMap((s) => [
    ...(s.key === DAIMON ? [[`${s.key}/*`, "All Daimon tools"] as [string, string]] : []),
    ...s.tools.map((t): [string, string] => [`${s.key}/${t.name}`, `${s.name} / ${t.name}`]),
  ])
  const apply = (r: Pick<AgentToolRule, "tool" | "rule" | "scope"> & { revision: number }) =>
    set.mutate({ tool: r.tool, rule: r.rule, scope: r.scope, expected_revision: r.revision })

  if (data.isError) return <Section title="Tool rules"><div className="text-sm text-muted-foreground">{data.error.message}</div></Section>
  return (
    <Section title="Tool rules">
      <div className="space-y-3">
        {(data.data?.rules ?? []).filter((r) => r.rule).map((r) => (
          <div key={r.tool} className="space-y-1.5">
            <Mono className="block truncate text-xs">{r.tool}</Mono>
            <div className="flex flex-wrap items-center gap-1.5">
              <Pick value={r.rule ?? ""} options={Object.entries(RULES) as [Rule, string][]} label="Rule" disabled={!active || set.isPending} className="w-24" onChange={(rule) => apply({ ...r, rule })} />
              <Pick value={r.scope} options={Object.entries(SCOPES) as [AgentToolRule["scope"], string][]} label="Applies to" disabled={!active || set.isPending} className="w-44" onChange={(scope) => apply({ ...r, scope })} />
              <Button size="xs" variant="outline" disabled={!active || set.isPending} onClick={() => apply({ ...r, rule: null })}>Clear</Button>
            </div>
          </div>
        ))}
        {data.data && !data.data.rules.some((r) => r.rule) && <div className="text-sm text-muted-foreground">None</div>}
        {data.data && !active && <div className="text-xs text-muted-foreground">Read only</div>}
        {active && (
          <div className="space-y-1.5">
            <Pick value={draft.tool} options={tools.filter(([k]) => !data.data?.rules.some((r) => r.tool === k && r.rule))} label="Tool" onChange={(tool) => setDraft({ ...draft, tool })} />
            <div className="flex flex-wrap items-center gap-1.5">
              <Pick value={draft.rule} options={Object.entries(RULES) as [Rule, string][]} label="Rule" className="w-24" onChange={(rule) => setDraft({ ...draft, rule })} />
              <Pick value={draft.scope} options={Object.entries(SCOPES) as [AgentToolRule["scope"], string][]} label="Applies to" className="w-44" onChange={(scope) => setDraft({ ...draft, scope })} />
              <Button size="sm" disabled={!draft.tool || !draft.rule || set.isPending}
                onClick={() => apply({ tool: draft.tool, rule: draft.rule as Rule, scope: draft.scope, revision: data.data?.rules.find((r) => r.tool === draft.tool)?.revision ?? 0 })}>Add</Button>
            </div>
          </div>
        )}
      </div>
    </Section>
  )
}

/** The Google Workspace identity the agent acts as, when its operator bound one. */
export function GoogleBadge({ agent }: Pick<Props, "agent">) {
  const g = useGoogleBinding(agent.id, agent.can_view_details)
  if (!g.data) return null
  return (
    <Section title="Google Workspace">
      <div className="space-y-1.5 text-sm">
        <Mono className="block truncate">{g.data.email}</Mono>
        <div className="flex flex-wrap gap-1">
          {g.data.scopes.map((s) => <Pill key={s}>{s.split("/").pop()}</Pill>)}
        </div>
      </div>
    </Section>
  )
}

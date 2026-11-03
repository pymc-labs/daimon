import { X } from "lucide-react"
import { useId, useState } from "react"

import { useConfirm } from "@/lib/confirm"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useMe } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { useAgents } from "@/modules/agents/api"
import { useChannelDefaults, useDefaultsSave, useSandboxNames, type DefaultsKey, type Outcome } from "./api"
import { defaultSource, SWITCH_VALUE, switchChange, unavailableText } from "./lib"
import { Notice } from "./shared"
import type { ChannelDefaults, ChannelSwitch } from "./types"

const CLEAR = "__clear"

function Field({ label, source, children }: { label: string; source?: string | null; children: React.ReactNode }) {
  return (
    <div className="min-w-0 space-y-1">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="flex min-h-8 items-center text-sm font-medium">{children}</div>
      <div className="h-4 text-xs text-faint">{source}</div>
    </div>
  )
}

function AgentField({ d, edit, run, busy }: { d: ChannelDefaults; edit: boolean; run: (k: DefaultsKey, b: unknown) => void; busy: boolean }) {
  const { data: agents = [] } = useAgents()
  const own = d.agent.source === "workspace" ? d.agent.value : null
  const names = [...new Set([...(d.agent.value ? [d.agent.value] : []), ...agents.map((a) => a.name)])]
  return (
    <Field label="Default agent" source={defaultSource(d.agent)}>
      {edit ? (
        <Select value={d.agent.value ?? ""} disabled={busy} onValueChange={(v) => {
          if (v === CLEAR) run("agent", { agent: null, expected: own })
          else if (v !== d.agent.value) run("agent", { agent: v, expected: own })
        }}>
          <SelectTrigger size="sm" className="w-full" aria-label="Default agent"><SelectValue placeholder="None" /></SelectTrigger>
          <SelectContent>
            {names.map((n) => <SelectItem key={n} value={n}>{n}</SelectItem>)}
            {own && <SelectItem value={CLEAR}>Built-in default</SelectItem>}
          </SelectContent>
        </Select>
      ) : <span className="truncate">{d.agent.value ?? "None"}</span>}
    </Field>
  )
}

function SandboxField({ d, edit, run, busy }: { d: ChannelDefaults; edit: boolean; run: (k: DefaultsKey, b: unknown) => void; busy: boolean }) {
  const names = useSandboxNames()
  const list = useId()
  const own = d.environment.source === "workspace" ? d.environment.value : null
  const [draft, setDraft] = useState(d.environment.value ?? "")
  const commit = () => {
    const v = draft.trim()
    if (v && v !== d.environment.value) run("environment", { environment: v, expected: own })
    else setDraft(d.environment.value ?? "")
  }
  return (
    <Field label="Sandbox" source={defaultSource(d.environment)}>
      {edit ? (
        <div className="relative w-full">
          <Input
            list={list} value={draft} disabled={busy} aria-label="Sandbox" className={cn("h-8", own && "pr-8")}
            onChange={(e) => setDraft(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
          />
          <datalist id={list}>{names.map((n) => <option key={n} value={n} />)}</datalist>
          {own && (
            <button
              type="button" aria-label="Use built-in default" disabled={busy}
              className="absolute top-1/2 right-1.5 -translate-y-1/2 rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
              onClick={() => { setDraft(""); run("environment", { environment: null, expected: own }) }}
            ><X className="size-3.5" /></button>
          )}
        </div>
      ) : <span className="truncate">{d.environment.value ?? "None"}</span>}
    </Field>
  )
}

function SwitchField({ label, what, s, platform, edit, run, busy }: {
  label: string; what: "participation" | "progress_plans"; s: ChannelSwitch; platform: string
  edit: boolean; run: (k: DefaultsKey, b: unknown) => void; busy: boolean
}) {
  const key = what === "participation" ? "participation" : "progress-plans"
  if (!s.available) return <Field label={label}><span className="text-muted-foreground">{unavailableText(what, platform)}</span></Field>
  return (
    <Field label={label} source={defaultSource(s)}>
      {edit ? (
        <Select value={s.value} disabled={busy} onValueChange={(v) => {
          if (v === CLEAR) run(key, switchChange("inherit", s))
          else if (v !== s.value) run(key, switchChange(v as "on" | "off", s))
        }}>
          <SelectTrigger size="sm" className="w-full" aria-label={label}><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="on">On</SelectItem>
            <SelectItem value="off">Off</SelectItem>
            {s.own && <SelectItem value={CLEAR}>Built-in default</SelectItem>}
          </SelectContent>
        </Select>
      ) : <span>{SWITCH_VALUE[s.value]}</span>}
    </Field>
  )
}

/** The workspace's defaults above the channel list; admins change them. */
export function Defaults() {
  const { data: d } = useChannelDefaults()
  const { data: me } = useMe()
  const save = useDefaultsSave()
  const confirm = useConfirm()
  const [out, setOut] = useState<Outcome | null>(null)
  if (!d) return null
  const edit = !!me?.capabilities.includes("channels.manage")
  const run = (key: DefaultsKey, body: unknown) => void save.run(key, body, (message) =>
    confirm({ title: "Open network access?", body: message, action: "Continue", destructive: true })).then(setOut)
  const p = { edit, run, busy: save.pending }

  return (
    <div className="px-4 pt-4 pb-3 lg:px-6">
      <div className="grid grid-cols-2 gap-x-4 gap-y-3 lg:max-w-4xl lg:grid-cols-4">
        <AgentField d={d} {...p} />
        <SandboxField key={d.environment.value} d={d} {...p} />
        <SwitchField label="Following threads" what="participation" s={d.participation} platform={d.platform} {...p} />
        <SwitchField label="Progress plans" what="progress_plans" s={d.progress_plans} platform={d.platform} {...p} />
      </div>
      <Notice out={out} />
    </div>
  )
}


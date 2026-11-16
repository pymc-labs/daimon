import { useQueryClient } from "@tanstack/react-query"
import { ArrowDown, ArrowUp, Lock, Trash2, User, Users } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { AgentAvatar } from "@/components/common"
import { Facts, PanelSection, Tag } from "@/components/panel"
import { MultiPicker } from "@/components/picker"
import { SettingsList } from "@/components/settings"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useChatAgents } from "@/lib/hooks"
import type { AgentRef } from "@/lib/types"
import { cn } from "@/lib/utils"
import { useFeatureSave, useFeatures } from "@/modules/features/api"
import { useDmExplain, useDmPeople, useDmSave, useDmSettings, usePlatformGroups } from "./dm"
import type { DmExplain, DmGrant, DmPerson, DmRule, DmSettings, DmSubject, DmWho } from "./types"

// Direct messages: who may DM each agent (Agent access), the default agent and rules (Workspace).

const WHO: [DmWho, string][] = [["members", "Everyone"], ["listed", "Only listed"], ["excluded", "Everyone except listed"]]
const SUBJECT: Record<DmSubject, string> = { user: "Person", group: "Group", everyone: "Everyone" }
const DEFAULT = "__default"
const ID = /^[\w:.@-]{1,100}$/

function useDebounced(text: string) {
  const [term, setTerm] = useState("")
  useEffect(() => {
    const t = setTimeout(() => setTerm(text.trim()), 200)
    return () => clearTimeout(t)
  }, [text])
  return term
}

/**
 * People or groups by platform id, as chips: found by name, or a pasted id (no spaces, a digit).
 * `single` keeps one pick.
 */
function IdPicker({ kind, value, onChange, single, disabled }: {
  kind: "user" | "group"; value: DmPerson[]; onChange: (next: DmPerson[]) => void; single?: boolean; disabled?: boolean
}) {
  const [text, setText] = useState("")
  const term = useDebounced(text)
  const people = useDmPeople(kind === "user" ? term : "")
  const groups = usePlatformGroups(kind === "group")
  const named = new Map((groups.data?.groups ?? []).map((g) => [g.id, g.name]))
  const found = kind === "user"
    ? (term ? people.data ?? [] : [])
    : (groups.data?.groups ?? []).filter((g) => `${g.name ?? ""} ${g.id}`.toLowerCase().includes(term.toLowerCase()))
  const typed = ID.test(term) && /\d/.test(term) ? [{ id: term, name: null }] : []
  const label = (p: DmPerson) => p.name ?? named.get(p.id) ?? p.id
  const Icon = kind === "user" ? User : Users
  const known = new Map([...value, ...found, ...typed].map((p) => [p.id, { ...p, name: p.name ?? named.get(p.id) ?? null }]))
  const items = [...known.values()].map((p) => ({ id: p.id, label: label(p), icon: <Icon className="size-3.5 text-muted-foreground" /> }))
  const problem = kind === "group" && groups.data?.problem
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-1.5">
      <MultiPicker
        items={items} value={value.map((p) => p.id)} onSearch={setText} disabled={disabled}
        placeholder={kind === "user" ? "Find a person or paste an id" : "Find a group or paste an id"}
        addLabel={single ? "Choose" : "Add"}
        onAdd={(id) => { const p = known.get(id)!; onChange(single ? [p] : [...value, p]) }}
        onRemove={(id) => onChange(value.filter((p) => p.id !== id))}
      />
      {problem && <Tag>{problem === "reinstall" ? "Reinstall to list groups" : "Groups unavailable"}</Tag>}
    </span>
  )
}

/** On or off at the agent tier, or the value with a lock when a level above pins it. */
function DmSwitch({ agent, on }: { agent: AgentRef; on: boolean }) {
  const place = { agent: agent.id }
  const doc = useFeatures(place)
  const save = useFeatureSave(place)
  const qc = useQueryClient()
  const v = doc.data?.features.find((f) => f.key === "direct_messages")
  const value = v ? v.value === "on" : on
  if (v?.locked && v.source !== "agent") {
    return (
      <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className="inline-flex items-center gap-1.5"><Lock className="size-3.5 text-muted-foreground" aria-label="Locked" />{value ? "On" : "Off"}</span>
        <span className="text-xs text-muted-foreground">Locked by the {v.source === "deployment" ? "operator" : "workspace"}</span>
      </span>
    )
  }
  const own = v?.tiers.find((t) => t.tier === "agent")
  const flip = async (next: boolean) => {
    const error = await save.run({ key: "direct_messages", tier: "agent", value: next ? "on" : "off", locked: own?.locked ?? false, expected: own?.text ?? "default" })
    if (error) toast.error(error)
    void qc.invalidateQueries({ queryKey: ["admin", "agent-access"] })
  }
  return <Switch checked={value} disabled={!v || save.pending} onCheckedChange={(x) => void flip(x)} aria-label={`Direct messages for ${agent.name}`} />
}

/** Large radio cards for who may DM. */
function WhoCards({ who, disabled, onChange }: { who: DmWho; disabled?: boolean; onChange: (who: DmWho) => void }) {
  return (
    <div role="radiogroup" aria-label="Who can DM" className="space-y-2">
      {WHO.map(([v, label]) => {
        const on = v === who
        return (
          <button
            key={v} type="button" role="radio" aria-checked={on} disabled={disabled} onClick={() => !on && onChange(v)}
            className={cn("flex min-h-12 w-full items-center gap-3 rounded-lg border px-3 py-2 text-left text-sm enabled:hover:bg-accent disabled:opacity-50", on && "border-primary bg-accent")}
          >
            <span className={cn("flex size-4 shrink-0 items-center justify-center rounded-full border", on && "border-primary")}>
              {on && <span className="size-2 rounded-full bg-primary" />}
            </span>
            <span className="font-medium">{label}</span>
          </button>
        )
      })}
    </div>
  )
}

/** An agent's DM switch and who may DM it, for the Agent access drawer. */
export function AgentDm({ agent, dm, save }: { agent: AgentRef; dm: DmGrant; save: (next: DmGrant) => void }) {
  const change = (c: Partial<DmGrant>) => save({ ...dm, ...c })
  return (
    <>
      <PanelSection>
        <Facts rows={[["Direct messages", <DmSwitch key="on" agent={agent} on={dm.on} />]]} />
      </PanelSection>
      <PanelSection title="Who can DM">
        <WhoCards who={dm.who} disabled={!dm.on} onChange={(who) => change({ who })} />
        {dm.who !== "members" && (
          <div className="mt-3">
            <Facts rows={[
              ["People", <IdPicker key="u" kind="user" value={dm.users} disabled={!dm.on} onChange={(users) => change({ users })} />],
              ["Groups", <IdPicker key="g" kind="group" value={dm.groups.map((id) => ({ id, name: null }))} disabled={!dm.on} onChange={(g) => change({ groups: g.map((x) => x.id) })} />],
            ]} />
          </div>
        )}
      </PanelSection>
    </>
  )
}

function AgentSelect({ value, agents, none, label, onChange }: {
  value: string | null; agents: { id: string; name: string }[]; none: string; label: string; onChange: (agent: string | null) => void
}) {
  const { data: chat } = useChatAgents()
  const avatar = new Map((chat ?? []).map((a) => [a.id, a.avatar]))
  return (
    <Select value={value ?? DEFAULT} onValueChange={(v) => onChange(v === DEFAULT ? null : v)}>
      <SelectTrigger size="sm" className="w-44" aria-label={label}><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value={DEFAULT}>{none}</SelectItem>
        {agents.map((a) => <SelectItem key={a.id} value={a.name}><AgentAvatar src={avatar.get(a.id) ?? ""} name={a.name} className="size-4 rounded" />{a.name}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

/** The workspace's DM default agent, ordered rules and a "Check a person" box; nothing without DMs. */
export function WorkspaceDm() {
  const q = useDmSettings()
  return q.data?.platform ? <DmSettingsView s={q.data} /> : null
}

export function DmSettingsView({ s }: { s: DmSettings }) {
  const save = useDmSave()
  return (
    <>
      <SettingsList title="Direct messages" rows={[{
        label: "Default agent",
        control: <AgentSelect value={s.default_agent} agents={s.agents} none="None" label="Default agent" onChange={save.setDefault} />,
      }]} />
      <Rules s={s} save={save} />
      <CheckPerson />
    </>
  )
}

type Save = ReturnType<typeof useDmSave>

function Rules({ s, save }: { s: DmSettings; save: Save }) {
  const groups = usePlatformGroups(s.rules.some((r) => r.subject === "group"))
  const named = new Map((groups.data?.groups ?? []).map((g) => [g.id, g.name]))
  const who = (r: DmRule) => r.subject === "everyone" ? "Everyone" : (r.name ?? named.get(r.subject_id!) ?? r.subject_id!)
  const last = s.rules.length - 1
  return (
    <section className="mx-auto w-full max-w-3xl">
      <h2 className="mb-2 text-sm font-semibold">DM rules</h2>
      <div className="rounded-lg border">
        {s.rules.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-10 pl-4">#</TableHead><TableHead>Who</TableHead><TableHead className="w-48">Agent</TableHead>
                <TableHead className="w-20">Locked</TableHead><TableHead className="w-28" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {s.rules.map((r) => {
                const n = r.position + 1
                const Icon = r.subject === "user" ? User : Users
                return (
                  <TableRow key={`${r.position}:${r.subject}:${r.subject_id}`} className="hover:bg-transparent">
                    <TableCell className="pl-4 text-muted-foreground tabular">{n}</TableCell>
                    <TableCell className="max-w-56">
                      <span className="flex min-w-0 items-center gap-2"><Icon className="size-3.5 shrink-0 text-muted-foreground" /><span className="truncate">{who(r)}</span></span>
                    </TableCell>
                    <TableCell>
                      <AgentSelect
                        value={r.agent} agents={s.agents} none="Default agent" label={`Agent for rule ${n}`}
                        onChange={(agent) => save.change(r.position, agent ? { agent } : { agent: null, locked: false })}
                      />
                    </TableCell>
                    <TableCell>
                      <Switch checked={r.locked} disabled={!r.agent || save.pending} onCheckedChange={(locked) => save.change(r.position, { locked })} aria-label={`Lock rule ${n}`} />
                    </TableCell>
                    <TableCell className="pr-3">
                      <span className="flex justify-end gap-0.5">
                        <Button variant="ghost" size="icon-sm" aria-label={`Move rule ${n} up`} disabled={r.position === 0 || save.pending} onClick={() => save.change(r.position, { to: r.position - 1 })}><ArrowUp /></Button>
                        <Button variant="ghost" size="icon-sm" aria-label={`Move rule ${n} down`} disabled={r.position === last || save.pending} onClick={() => save.change(r.position, { to: r.position + 1 })}><ArrowDown /></Button>
                        <Button variant="ghost" size="icon-sm" aria-label={`Remove rule ${n}`} disabled={save.pending} onClick={() => save.remove(r.position)}><Trash2 /></Button>
                      </span>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
        <AddRule s={s} save={save} className={cn(s.rules.length > 0 && "border-t")} />
      </div>
    </section>
  )
}

function AddRule({ s, save, className }: { s: DmSettings; save: Save; className?: string }) {
  const [subject, setSubject] = useState<DmSubject>("group")
  const [picked, setPicked] = useState<DmPerson[]>([])
  const [agent, setAgent] = useState<string | null>(null)
  const [locked, setLocked] = useState(false)
  const id = subject === "everyone" ? null : picked[0]?.id
  const add = () => {
    save.add({ subject, subject_id: id ?? null, agent, locked: !!agent && locked })
    setPicked([])
  }
  return (
    <div className={cn("space-y-3 p-4", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <Select value={subject} onValueChange={(v) => { setSubject(v as DmSubject); setPicked([]) }}>
          <SelectTrigger size="sm" className="w-32" aria-label="Rule for"><SelectValue /></SelectTrigger>
          <SelectContent>{Object.entries(SUBJECT).map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}</SelectContent>
        </Select>
        {subject !== "everyone" && <IdPicker key={subject} kind={subject} single value={picked} onChange={setPicked} />}
        <span className="ml-auto flex items-center gap-2">
          <AgentSelect value={agent} agents={s.agents} none="Default agent" label="Agent for the new rule" onChange={(a) => { setAgent(a); if (!a) setLocked(false) }} />
          <label className="flex items-center gap-2 text-sm">
            <Switch checked={locked} disabled={!agent} onCheckedChange={setLocked} aria-label="Lock the new rule" />Locked
          </label>
        </span>
      </div>
      <div className="flex justify-end">
        <Button size="sm" className="max-sm:flex-1" disabled={(subject !== "everyone" && !id) || save.pending} onClick={add}>Add rule</Button>
      </div>
    </div>
  )
}

const ROUTE: Record<DmExplain["route"], string> = { start: "", choose: "Their choice", refused: "No agent", unchecked: "Unknown" }

/** Why a DM goes where it goes, in a few words. */
function why(e: DmExplain): string {
  const rule = e.rule === null ? "A rule" : `Rule ${e.rule + 1}`
  switch (e.reason) {
    case "locked": return `${rule}, locked`
    case "rule": return rule
    case "default": return "Default agent"
    case "only": return "The only agent they can use"
    case "pick": return "Their last pick"
    case "no_agent": return "No agent lets them in"
    case "locked_unavailable": return `${rule} names an agent they can't use`
    default: return e.route === "unchecked" ? "Couldn't read their groups" : ""
  }
}

function CheckPerson() {
  const [person, setPerson] = useState<DmPerson[]>([])
  const explain = useDmExplain()
  const e = explain.data
  const groups = usePlatformGroups(!!e?.groups?.length)
  const named = new Map((groups.data?.groups ?? []).map((g) => [g.id, g.name]))
  const held = e?.groups === null ? "Unknown" : e?.groups.map((g) => named.get(g) ?? g).join(", ") || "None"
  return (
    <section className="mx-auto w-full max-w-3xl">
      <h2 className="mb-2 text-sm font-semibold">Check a person</h2>
      <div className="space-y-3 rounded-lg border p-4">
        <Facts rows={[
          ["Person", <IdPicker key="p" kind="user" single value={person} onChange={(p) => { setPerson(p); explain.reset() }} />],
        ]} />
        {e && (
          <div className="border-t pt-3">
            <Facts rows={[
              ["Goes to", e.route === "start" ? e.agent : e.route === "choose" ? `${ROUTE.choose}: ${e.agents.join(", ")}` : ROUTE[e.route]],
              ["Why", why(e)],
              ["Can use", e.allowed.join(", ") || "No agent"],
              ["Groups", held],
            ]} />
          </div>
        )}
        <div className="flex justify-end">
          <Button
            size="sm" className="max-sm:flex-1" disabled={!person.length || explain.isPending}
            onClick={() => explain.mutate({ user: person[0]!.id })}
          >Check</Button>
        </div>
      </div>
    </section>
  )
}

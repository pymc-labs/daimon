// The data-server node: the server's agent for this agent, edited on the server as the signed-in person.
import { ExternalLink, Plus, RotateCcw, Trash2, X } from "lucide-react"
import { useState } from "react"

import { Ago, Mono, StatusDot } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { useConfirm } from "@/lib/confirm"
import { absolute, ago, shortDate } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useDataServerEdit, useDataServerLink, useDataServerScopes, useDataServerStatus, useDataServerTools } from "../api"
import { Pill } from "../bits"
import { Field, Section } from "../inspector/parts"
import type { ActsFor, DataServerAgent, DataServerStatus, DataServerTool, DataServerTools } from "../types"
import type { NodeCardProps, NodePanelProps } from "./registry"

type State = "connected" | "not_linked" | "unreachable"

const stateOf = (s: DataServerStatus): State =>
  !s.reachable ? "unreachable" : s.agent?.can_manage ? "connected" : "not_linked"

const STATE: Record<State, { dot: "healthy" | "setup" | "failing"; label: string }> = {
  connected: { dot: "healthy", label: "Connected" },
  not_linked: { dot: "setup", label: "Not linked" },
  unreachable: { dot: "failing", label: "Unreachable" },
}

const ACTS_FOR: Record<ActsFor, string> = { requester: "Requester", self: "Itself" }

const usableCount = (t: DataServerTools | undefined) => t?.tools.filter((x) => x.usable).length

export function DataServerCard({ agent }: NodeCardProps) {
  const { data: s } = useDataServerStatus(agent.id)
  const linked = !!s && stateOf(s) === "connected"
  const { data: tools } = useDataServerTools(agent.id, linked)
  if (!s) return <div className="space-y-2 p-3"><Skeleton className="h-4 w-24" /><Skeleton className="h-4 w-32" /></div>
  const state = stateOf(s)
  const a = s.agent
  return (
    <div className="grid grid-cols-[76px_1fr] items-center gap-y-2 px-3 py-3 text-xs">
      <span className="text-muted-foreground">State</span>
      <StatusDot status={STATE[state].dot} label={STATE[state].label} />
      {linked && a && (
        <>
          <span className="text-muted-foreground">Scopes</span>
          <span className="tabular">{a.scopes.length}</span>
          <span className="text-muted-foreground">Tools</span>
          <span className="tabular">{usableCount(tools) ?? "–"}</span>
          <span className="text-muted-foreground">Last call</span>
          <Ago iso={a.last_call_at} />
        </>
      )}
    </div>
  )
}

export function DataServerPanel({ agent, node, ro, select }: NodePanelProps) {
  const status = useDataServerStatus(agent.id)
  const link = useDataServerLink(agent.id)
  const s = status.data
  const state = s && stateOf(s)
  return (
    <>
      <div className="flex items-center gap-3 px-5 py-4">
        <div className="min-w-0 flex-1">
          <div className="truncate text-base font-semibold">{s?.agent?.name ?? node.label}</div>
          <div className="text-xs text-muted-foreground">{node.label}</div>
        </div>
        {state && <StatusDot status={STATE[state].dot} label={STATE[state].label} />}
      </div>
      {!s ? <div className="space-y-2 px-5"><Skeleton className="h-8" /><Skeleton className="h-24" /></div>
        : state === "unreachable" ? (
          <Section>
            <div className="flex items-center gap-3 text-sm">
              <span className="min-w-0 flex-1 text-muted-foreground">{s.problem}</span>
              <Button variant="outline" size="sm" disabled={status.isFetching} onClick={() => void status.refetch()}><RotateCcw />Retry</Button>
            </div>
          </Section>
        ) : state === "not_linked" ? (
          <Section>
            <div className="flex items-center gap-3 text-sm">
              <span className="min-w-0 flex-1 text-muted-foreground">{s.agent?.manager ? `Managed by ${s.agent.manager}` : "No manager"}</span>
              {!ro && <Button size="sm" disabled={link.connect.isPending} onClick={() => link.connect.mutate()}>Link</Button>}
            </div>
          </Section>
        ) : s.agent && <Linked agentId={agent.id} a={s.agent} status={s} label={node.label} />}
      {!ro && (
        <div className="mt-auto border-t px-5 py-3">
          <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-destructive" disabled={link.disconnect.isPending}
            onClick={() => link.disconnect.mutate(undefined, { onSuccess: () => select("") })}>
            <Trash2 />Disconnect
          </Button>
        </div>
      )}
    </>
  )
}

function Linked({ agentId, a, status, label }: { agentId: string; a: DataServerAgent; status: DataServerStatus; label: string }) {
  const edit = useDataServerEdit(agentId)
  const confirm = useConfirm()
  const patch = edit.update.mutate
  const [now] = useState(() => Date.now())
  const tokens = Array.isArray(a.tokens) ? a.tokens.filter((t) => !t.revoked_at && !(t.expires_at && Date.parse(t.expires_at) < now)) : []
  const suspend = async () => {
    if (a.status === "suspended") return patch({ status: "active" })
    if (await confirm({ title: `Suspend ${a.name}?`, body: "Its tokens stop working.", action: "Suspend", destructive: true })) patch({ status: "suspended" })
  }
  return (
    <>
      <Section title="Access">
        <div className="space-y-3">
          <Field label="Acts for">
            <Select value={a.acts_for} onValueChange={(v) => patch({ acts_for: v as ActsFor })}>
              <SelectTrigger size="sm" className="h-8 w-36"><SelectValue /></SelectTrigger>
              <SelectContent>{(Object.keys(ACTS_FOR) as ActsFor[]).map((v) => <SelectItem key={v} value={v}>{ACTS_FOR[v]}</SelectItem>)}</SelectContent>
            </Select>
          </Field>
          <Field label="Can write"><Switch checked={a.can_write} onCheckedChange={(v) => patch({ can_write: v })} /></Field>
          <Field label="Hide clients"><Switch checked={a.redact_clients} onCheckedChange={(v) => patch({ redact_clients: v })} /></Field>
          <Field label="Personal"><Switch checked={a.personal} onCheckedChange={(v) => patch({ personal: v })} /></Field>
          {status.admin && (
            <Field label="Admin">
              <Switch checked={a.role === "admin"} onCheckedChange={async (v) => {
                if (!v || await confirm({ title: `Make ${a.name} admin?`, action: "Make admin" })) patch({ role: v ? "admin" : "member" })
              }} />
            </Field>
          )}
          <Field label="Status">
            <span className="flex items-center gap-3 text-sm">
              {a.status === "suspended" ? "Suspended" : "Active"}
              <Button size="xs" variant="outline" className="ml-auto" onClick={() => void suspend()}>{a.status === "suspended" ? "Reactivate" : "Suspend"}</Button>
            </span>
          </Field>
        </div>
      </Section>
      <Scopes agentId={agentId} a={a} />
      <Tools agentId={agentId} canWrite={a.can_write} admin={status.admin} />
      <Section
        title="Tokens"
        action={status.open_url && (
          <Button variant="outline" size="xs" asChild>
            <a href={status.open_url} target="_blank" rel="noreferrer">Open in {label}<ExternalLink /></a>
          </Button>
        )}
      >
        {!tokens.length ? <div className="text-sm text-muted-foreground">None</div> : tokens.map((t) => (
          <div key={t.id} className="flex h-8 items-center gap-2 text-sm">
            <Mono className="shrink-0 text-muted-foreground">{t.prefix}</Mono>
            <span className="shrink-0">{t.name}</span>
            <span className="min-w-0 truncate text-xs text-muted-foreground">
              {t.last_used_at ? `Used ${ago(t.last_used_at)}` : "Never used"}{t.expires_at && `, expires ${shortDate(t.expires_at)}`}
            </span>
            <Button size="xs" variant="ghost" className="ml-auto text-muted-foreground" onClick={async () => {
              if (await confirm({ title: `Revoke ${t.name}?`, body: "It stops working at once.", action: "Revoke", destructive: true })) edit.revokeToken.mutate(t.id)
            }}>Revoke</Button>
          </div>
        ))}
      </Section>
      <Section title="Activity">
        {!a.activity?.length ? <div className="text-sm text-muted-foreground">None</div> : a.activity.map((x, i) => (
          <div key={i} className="flex h-7 items-center gap-2 text-sm" title={absolute(x.at)}>
            <span className="w-14 shrink-0 text-xs text-muted-foreground">{ago(x.at)}</span>
            <span className="shrink-0 font-medium">{x.action}</span>
            <span className="truncate text-muted-foreground">{x.target}</span>
            {x.requester && <span className="shrink-0 text-xs text-faint">for {x.requester}</span>}
            <span className="ml-auto shrink-0 text-xs text-faint uppercase">{x.via}</span>
          </div>
        ))}
      </Section>
    </>
  )
}

function Scopes({ agentId, a }: { agentId: string; a: DataServerAgent }) {
  const { data: visible } = useDataServerScopes(agentId)
  const edit = useDataServerEdit(agentId)
  const [open, setOpen] = useState(false)
  const name = (id: string) => visible?.find((s) => s.id === id)?.name ?? id
  const addable = (visible ?? []).filter((s) => !a.scopes.includes(s.id))
  const set = (scopes: string[]) => edit.update.mutate({ scopes })
  const x = "ml-auto rounded p-1 text-faint hover:bg-accent hover:text-foreground"
  return (
    <Section
      title={`Scopes (${a.scopes.length})`}
      action={addable.length > 0 && (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild><Button variant="outline" size="xs"><Plus />Add</Button></PopoverTrigger>
          <PopoverContent className="w-64 p-0" align="end">
            <Command>
              <CommandInput placeholder="Search scopes" />
              <CommandList className="max-h-64">
                <CommandEmpty>No match</CommandEmpty>
                <CommandGroup>
                  {addable.map((s) => (
                    <CommandItem key={s.id} value={`${s.name} ${s.id}`} onSelect={() => { set([...a.scopes, s.id]); setOpen(false) }}>
                      <span className="truncate">{s.name}</span><span className="ml-auto text-xs text-muted-foreground">{s.kind}</span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
      )}
    >
      {!a.scopes.length && !a.team_scopes.length && <div className="text-sm text-muted-foreground">None</div>}
      {a.scopes.map((s) => (
        <div key={s} className="flex h-8 items-center gap-2 text-sm">
          <span className="truncate">{name(s)}</span>
          <button onClick={() => set(a.scopes.filter((y) => y !== s))} className={x} aria-label={`Remove ${name(s)}`}><X className="size-3.5" /></button>
        </div>
      ))}
      {a.team_scopes.filter((s) => !a.scopes.includes(s)).map((s) => (
        <div key={s} className="flex h-8 items-center gap-2 text-sm">
          <span className="truncate text-muted-foreground">{name(s)}</span><Pill className="ml-auto">Group</Pill>
        </div>
      ))}
    </Section>
  )
}

function Tools({ agentId, canWrite, admin }: { agentId: string; canWrite: boolean; admin: boolean }) {
  const { data } = useDataServerTools(agentId)
  const edit = useDataServerEdit(agentId)
  const [open, setOpen] = useState(false)
  if (!data) return <Section title="Tools"><Skeleton className="h-16" /></Section>
  const put = edit.setTool.mutate
  // An admin's remove sticks for everyone else; a write tool needs the agent to write.
  const stuck = (r: { admin: boolean; allow: boolean | null }) => !admin && r.admin && r.allow === false
  const blocked = (t: DataServerTool) => t.writes && !canWrite
  const added = (t: DataServerTool) => t.allow === true && !t.whole
  const wholes = data.connections.filter((c) => c.allow !== null)
  const shown = data.tools.filter((t) => t.usable || (!t.whole && t.allow === false) || (added(t) && blocked(t)))
  const addable = data.tools.filter((t) => t.grantable && !t.usable && !stuck(t) && !(added(t) && blocked(t)))
  const conns = data.connections.filter((c) => c.grantable && c.allow !== true && !stuck(c))
  const pick = (b: { connection_id: string; tool_name?: string }) => { put({ ...b, allow: true }); setOpen(false) }
  const x = "ml-auto rounded p-1 text-faint hover:bg-accent hover:text-foreground"
  const by = (r: { allow: boolean | null; granted_by: string | null }) => `${r.allow ? "Added" : "Removed"}${r.granted_by ? ` by ${r.granted_by}` : ""}`
  return (
    <Section
      title={`Tools (${data.tools.filter((t) => t.usable).length})`}
      action={(addable.length > 0 || conns.length > 0) && (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild><Button variant="outline" size="xs"><Plus />Add</Button></PopoverTrigger>
          <PopoverContent className="w-72 p-0" align="end">
            <Command>
              <CommandInput placeholder="Search tools" />
              <CommandList className="max-h-72">
                <CommandEmpty>No match</CommandEmpty>
                {conns.length > 0 && (
                  <CommandGroup heading="All tools of">
                    {conns.map((c) => <CommandItem key={c.id} value={`all ${c.name}`} onSelect={() => pick({ connection_id: c.id })}>{c.name}</CommandItem>)}
                  </CommandGroup>
                )}
                {addable.length > 0 && (
                  <CommandGroup heading="Tools">
                    {addable.map((t) => (
                      <CommandItem key={`${t.connection_id}/${t.name}`} value={`${t.connection} ${t.name}`} disabled={blocked(t)}
                        onSelect={() => pick({ connection_id: t.connection_id, tool_name: t.name })}>
                        <Mono>{t.name}</Mono><span className="ml-auto text-xs text-muted-foreground">{blocked(t) ? "Needs write" : t.connection}</span>
                      </CommandItem>
                    ))}
                  </CommandGroup>
                )}
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
      )}
    >
      {!wholes.length && !shown.length && <div className="text-sm text-muted-foreground">None</div>}
      {wholes.map((c) => (
        <div key={c.id} className="flex h-8 items-center gap-2 text-sm">
          <span className="truncate font-medium">{c.name}</span><span className="shrink-0 text-muted-foreground">All tools</span>
          <span className="truncate text-xs text-muted-foreground">{by(c)}</span>
          {!stuck(c) && <button onClick={() => put({ connection_id: c.id, allow: null })} className={x} aria-label={`Reset ${c.name}`}><RotateCcw className="size-3.5" /></button>}
        </div>
      ))}
      {shown.map((t) => (
        <div key={`${t.connection_id}/${t.name}`} className="flex h-8 items-center gap-2 text-sm">
          <Mono className={cn("truncate", !t.usable && "text-muted-foreground line-through decoration-faint")}>{t.name}</Mono>
          <span className="shrink-0 text-xs text-muted-foreground">{t.connection}</span>
          {t.allow !== null && !t.whole && <span className="truncate text-xs text-muted-foreground">{by(t)}</span>}
          {!t.usable && added(t) && blocked(t) && <span className="shrink-0 text-xs text-muted-foreground">Needs write</span>}
          {!stuck(t) && (t.usable || added(t)
            ? <button onClick={() => put({ connection_id: t.connection_id, tool_name: t.name, allow: t.allow && !t.whole ? null : false })} className={x} aria-label={`Remove ${t.name}`}><X className="size-3.5" /></button>
            : <Button size="xs" variant="ghost" className="ml-auto text-muted-foreground" onClick={() => put({ connection_id: t.connection_id, tool_name: t.name, allow: null })}>Reset</Button>)}
        </div>
      ))}
    </Section>
  )
}

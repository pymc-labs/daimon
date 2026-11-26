import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CircleAlert, CircleCheck, EyeOff, Link2, Link2Off, Lock, Plus } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { Ago } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Facts, PanelHead, PanelMenu, PanelSection, type MenuAction } from "@/components/panel"
import { MultiPicker } from "@/components/picker"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { rowProps, useParam } from "@/lib/filters"
import { useDebounced } from "@/lib/hooks"
import { Pill } from "../agents/bits"
import type { Candidate, RedactionDetail, RedactionSet, Rules, Scope, SourceIdentity } from "../data/types"
import { Cards, Section } from "./support/parts"

const BASE = "/api/admin/data"
const KEY = ["admin", "data"]
const KINDS = { public: "Public", internal: "Internal", shared: "Shared", client: "Client", restricted: "Restricted" }
type Person = { account_id: string; name: string }
type Ref = Pick<SourceIdentity, "provider" | "workspace" | "subject">
const refOf = (i: Ref): Ref => ({ provider: i.provider, workspace: i.workspace, subject: i.subject })
const idOf = (i: Ref) => `${i.provider}|${i.workspace}|${i.subject}`

const usePeople = (q: string) => useQuery({ queryKey: [...KEY, "people", q], queryFn: () => api.get<Person[]>(`${BASE}/people?q=${encodeURIComponent(q)}`), placeholderData: (p) => p })

/** A person chosen by name from the workspace's people. */
function PersonPick({ label, icon, onPick, variant = "outline" }: { label: string; icon?: React.ReactNode; onPick: (p: Person) => void; variant?: "outline" | "ghost" }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState("")
  const found = usePeople(useDebounced(text.trim(), 200))
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild><Button size="sm" variant={variant}>{icon}{label}</Button></PopoverTrigger>
      <PopoverContent className="w-64 p-0" align="end">
        <Command shouldFilter={false}>
          <CommandInput placeholder="Find a person" value={text} onValueChange={setText} />
          <CommandList className="max-h-64">
            <CommandEmpty>No one found</CommandEmpty>
            <CommandGroup>
              {found.data?.map((p) => <CommandItem key={p.account_id} value={p.account_id} onSelect={() => { setOpen(false); onPick(p) }}>{p.name}</CommandItem>)}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

function useWrite(done?: string) {
  const qc = useQueryClient()
  return { qc, ok: () => { void qc.invalidateQueries({ queryKey: KEY }); if (done) toast(done) } }
}

export function DataAccess() {
  return (
    <div className="mx-auto max-w-[1040px] space-y-10">
      <Scopes />
      <Identities />
      <Redaction />
    </div>
  )
}

// Scopes and grants

function Scopes() {
  const q = useQuery({ queryKey: [...KEY, "scopes"], queryFn: () => api.get<Scope[]>(`${BASE}/scopes`) })
  const [, setScope] = useParam("scope")
  const [adding, setAdding] = useState(false)
  return (
    <Section title="Scopes" add={<Button size="sm" onClick={() => setAdding(true)}><Plus />New scope</Button>}>
      <QueryState query={q} empty="No scope yet">
        {(rows) => (
          <>
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Scope</TableHead><TableHead className="max-sm:hidden">Kind</TableHead><TableHead>Held by</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((s) => (
                  <TableRow key={s.id} {...rowProps(() => setScope(s.id))}>
                    <TableCell>
                      <span className="font-medium">{s.name}</span>
                      {s.bound && <Pill className="ml-2 gap-1"><Lock className="size-3" />Bound</Pill>}
                    </TableCell>
                    <TableCell className="text-muted-foreground max-sm:hidden">{KINDS[s.kind as keyof typeof KINDS] ?? s.kind}</TableCell>
                    <TableCell className="max-w-0 truncate text-muted-foreground">{s.holders.map((h) => h.name ?? h.holder).join(", ") || "No one"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Drawer param="scope" title="Scope" ids={rows.map((r) => r.id)}>
              {(id) => { const s = rows.find((r) => r.id === id); return s ? <ScopePanel s={s} /> : null }}
            </Drawer>
          </>
        )}
      </QueryState>
      {adding && <NewScope onClose={() => setAdding(false)} />}
    </Section>
  )
}

function ScopePanel({ s }: { s: Scope }) {
  const { ok } = useWrite()
  const confirm = useConfirm()
  const [, setScope] = useParam("scope")
  const [text, setText] = useState("")
  const found = usePeople(useDebounced(text.trim(), 200))
  const grant = useMutation({ mutationFn: (holder: string) => api.post(`${BASE}/scopes/${s.id}/grants`, { holder }), onSuccess: ok })
  const revoke = useMutation({ mutationFn: (holder: string) => api.del(`${BASE}/scopes/${s.id}/grants?holder=${encodeURIComponent(holder)}`), onSuccess: ok })
  const remove = useMutation({ mutationFn: () => api.del(`${BASE}/scopes/${s.id}`), onSuccess: () => { ok(); setScope(null); toast("Scope deleted") } })
  const items = [...(found.data ?? []).map((p) => ({ id: `account:${p.account_id}`, label: p.name })), ...s.holders.map((h) => ({ id: h.holder, label: h.name ?? h.holder }))]
  const menu: MenuAction[] = s.bound ? [] : [{
    label: "Delete scope", destructive: true, onSelect: async () => {
      if (await confirm({ title: `Delete ${s.name}?`, body: "Its grants are removed too.", action: "Delete", destructive: true })) remove.mutate()
    },
  }]
  return (
    <div>
      <PanelHead title={s.name} subtitle={s.id} tags={s.bound ? <Pill className="gap-1"><Lock className="size-3" />Bound</Pill> : undefined} menu={menu} />
      <PanelSection><Facts rows={[["Kind", KINDS[s.kind as keyof typeof KINDS] ?? s.kind], ["About", s.description]]} /></PanelSection>
      <PanelSection title="Held by">
        <MultiPicker
          items={items} value={s.holders.map((h) => h.holder)} onSearch={setText} placeholder="Find a person" unknown="Unknown" addLabel="Add"
          locked={s.holders.filter((h) => h.bound).map((h) => h.holder)} disabled={s.bound}
          onAdd={(id) => grant.mutate(id)} onRemove={(id) => revoke.mutate(id)}
        />
      </PanelSection>
    </div>
  )
}

function NewScope({ onClose }: { onClose: () => void }) {
  const { ok } = useWrite("Scope added")
  const [kind, setKind] = useState<keyof typeof KINDS>("internal")
  const [id, setId] = useState("")
  const [name, setName] = useState("")
  const [about, setAbout] = useState("")
  const create = useMutation({
    meta: { inline: true },
    mutationFn: () => api.post(`${BASE}/scopes`, { id: id.trim(), kind, name: name.trim(), description: about.trim() }),
    onSuccess: () => { ok(); onClose() },
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>New scope</DialogTitle></DialogHeader>
        <form id="new-scope" className="space-y-4" onSubmit={(e) => { e.preventDefault(); create.mutate() }}>
          <div className="space-y-1.5"><Label>Kind</Label><Cards label="Kind" value={kind} onChange={setKind} choices={KINDS} className="max-sm:!grid-cols-2" /></div>
          <div className="space-y-1.5"><Label htmlFor="scope-name">Name</Label><Input id="scope-name" autoComplete="off" maxLength={200} value={name} onChange={(e) => setName(e.target.value)} /></div>
          <div className="space-y-1.5"><Label htmlFor="scope-id">Id</Label><Input id="scope-id" autoComplete="off" maxLength={64} value={id} onChange={(e) => setId(e.target.value)} /></div>
          <div className="space-y-1.5"><Label htmlFor="scope-about">About</Label><Input id="scope-about" autoComplete="off" maxLength={500} value={about} onChange={(e) => setAbout(e.target.value)} /></div>
        </form>
        <DialogFooter className="max-sm:flex-row">
          <Button variant="outline" className="max-sm:flex-1" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="new-scope" className="max-sm:flex-1" disabled={!id.trim() || !name.trim() || create.isPending}>Add scope</Button>
        </DialogFooter>
        {create.error && <p role="alert" className="text-right text-sm text-destructive">{create.error.message}</p>}
      </DialogContent>
    </Dialog>
  )
}

// Identity links

function Identities() {
  const { ok } = useWrite()
  const confirm = useConfirm()
  const cands = useQuery({ queryKey: [...KEY, "candidates"], queryFn: () => api.get<Candidate[]>(`${BASE}/candidates`) })
  const all = useQuery({ queryKey: [...KEY, "identities"], queryFn: () => api.get<SourceIdentity[]>(`${BASE}/identities`) })
  const link = useMutation({ mutationFn: (b: Ref & { account_id: string }) => api.post(`${BASE}/links`, b), onSuccess: () => { ok(); toast("Linked") } })
  const reject = useMutation({ mutationFn: (b: Ref & { account_id: string }) => api.post(`${BASE}/links/reject`, b), onSuccess: () => { ok(); toast("Rejected") } })
  const unlink = useMutation({ mutationFn: (b: Ref) => api.post(`${BASE}/links/unlink`, b), onSuccess: () => { ok(); toast("Unlinked") } })
  const redact = useMutation({ mutationFn: (b: { id: string; redacted: boolean }) => api.put(`${BASE}/people/${b.id}/redacted`, { redacted: b.redacted }), onSuccess: ok })
  return (
    <Section title="Identity links">
      <QueryState query={all} empty="No source account yet">
        {(rows) => (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Account</TableHead><TableHead className="max-sm:hidden">Source</TableHead><TableHead>Person</TableHead>
                <TableHead className="max-md:hidden">Linked</TableHead><TableHead className="w-48" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((i) => {
                const cand = i.account_id ? undefined : cands.data?.find((c) => idOf(c) === idOf(i))
                const person = i.account_id ? (i.account_name ?? "Unknown person") : null
                const menu: MenuAction[] = !i.account_id ? [] : [
                  { label: i.redacted ? "Stop redacting" : "Redact for this person", icon: <EyeOff />, onSelect: () => redact.mutate({ id: i.account_id!, redacted: !i.redacted }) },
                  { label: "Unlink", icon: <Link2Off />, destructive: true, onSelect: async () => {
                    if (await confirm({ title: `Unlink ${i.name}?`, action: "Unlink", destructive: true })) unlink.mutate(refOf(i))
                  } },
                ]
                return (
                  <TableRow key={idOf(i)}>
                    <TableCell className="font-medium">{i.name}</TableCell>
                    <TableCell className="text-muted-foreground max-sm:hidden">{i.provider}</TableCell>
                    <TableCell>
                      {person ? <span className="inline-flex items-center gap-1.5"><CircleCheck className="size-3.5 text-success" />{person}{i.redacted && <Pill className="ml-1 gap-1"><EyeOff className="size-3" />Redacted</Pill>}</span>
                        : <span className="inline-flex items-center gap-1.5 text-muted-foreground"><CircleAlert className="size-3.5" />{cand ? (cand.account_name ?? cand.email) : "Unlinked"}</span>}
                    </TableCell>
                    <TableCell className="text-muted-foreground max-md:hidden">{i.linked_at ? <Ago iso={i.linked_at} /> : ""}</TableCell>
                    <TableCell className="text-right">
                      {cand ? (
                        <span className="inline-flex gap-1.5">
                          <Button size="sm" variant="ghost" disabled={reject.isPending} onClick={() => reject.mutate({ ...refOf(i), account_id: cand.account_id })}>Reject</Button>
                          <Button size="sm" disabled={link.isPending} onClick={() => link.mutate({ ...refOf(i), account_id: cand.account_id })}>Link</Button>
                        </span>
                      ) : !i.account_id
                        ? <PersonPick label="Link" variant="ghost" icon={<Link2 />} onPick={(p) => link.mutate({ ...refOf(i), account_id: p.account_id })} />
                        : <PanelMenu items={menu} />}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </QueryState>
    </Section>
  )
}

// Redaction sets

const count = (r: Rules | null) => r && [
  ["Aliases", Object.keys(r.aliases).length], ["Removals", r.removals.length], ["Patterns", r.regex.length],
  ["Excluded", r.exclude_ids.length], ["Entities", r.entities.length],
] as [string, number][]

function SetState({ s }: { s: RedactionSet }) {
  if (s.error) return <span className="inline-flex items-center gap-1.5 text-destructive"><CircleAlert className="size-3.5" />Error</span>
  if (s.pending) return <span className="inline-flex items-center gap-1.5 text-warning"><CircleAlert className="size-3.5" />Needs approval</span>
  return <span className="inline-flex items-center gap-1.5 text-success"><CircleCheck className="size-3.5" />Approved</span>
}

function Redaction() {
  const q = useQuery({ queryKey: [...KEY, "redaction"], queryFn: () => api.get<RedactionSet[]>(`${BASE}/redaction`) })
  const [, setSet] = useParam("set")
  return (
    <Section title="Redaction sets">
      <QueryState query={q} empty="No redaction set yet">
        {(rows) => (
          <>
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent"><TableHead>Set</TableHead><TableHead className="max-sm:hidden">From</TableHead><TableHead>State</TableHead><TableHead className="max-md:hidden">Checked</TableHead></TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((s) => (
                  <TableRow key={s.name} {...rowProps(() => setSet(s.name))}>
                    <TableCell className="font-medium">{s.name}</TableCell>
                    <TableCell className="break-all text-muted-foreground max-sm:hidden">{s.repo ? `${s.repo}/${s.path ?? ""}` : "Declared"}</TableCell>
                    <TableCell><SetState s={s} /></TableCell>
                    <TableCell className="text-muted-foreground max-md:hidden"><Ago iso={s.checked_at} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Drawer param="set" title="Redaction set" ids={rows.map((r) => r.name)}>{(name) => <SetPanel name={name} />}</Drawer>
          </>
        )}
      </QueryState>
    </Section>
  )
}

function SetPanel({ name }: { name: string }) {
  const { ok } = useWrite()
  const q = useQuery({ queryKey: [...KEY, "redaction", name], queryFn: () => api.get<RedactionDetail>(`${BASE}/redaction/${encodeURIComponent(name)}`) })
  const approve = useMutation({ mutationFn: (sha: string) => api.post(`${BASE}/redaction/${encodeURIComponent(name)}/approve`, { sha }), onSuccess: () => { ok(); toast("Approved") } })
  return (
    <QueryState query={q}>
      {({ info, rules, pending_rules }) => (
        <div>
          <PanelHead title={info.name} subtitle={<SetState s={info} />} actions={info.pending && info.pin_sha && <Button size="sm" disabled={approve.isPending} onClick={() => approve.mutate(info.pin_sha!)}>Approve</Button>} />
          <PanelSection>
            <Facts rows={[["Repository", info.repo], ["Path", info.path], ["Ref", info.ref], ["Approved", info.approved_sha?.slice(0, 10)], ["Waiting", info.pending ? info.pin_sha?.slice(0, 10) : null], ["SQL and file results", rules?.at === "answer" ? "Redacted best effort" : null], ["Error", info.error]]} />
          </PanelSection>
          {count(rules) && <PanelSection title="Rules"><Facts rows={count(rules)!.map(([k, n]) => [k, String(n)])} /></PanelSection>}
          {count(pending_rules) && <PanelSection title="Waiting rules"><Facts rows={count(pending_rules)!.map(([k, n]) => [k, String(n)])} /></PanelSection>}
        </div>
      )}
    </QueryState>
  )
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Archive, CircleAlert, CircleCheck, CirclePause, EyeOff, Lock, Plus, RefreshCw } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { Ago } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { Facts, PanelHead, PanelSection, type MenuAction } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { rowProps, useParam } from "@/lib/filters"
import { compact, plural } from "@/lib/format"
import { Cards } from "../admin/support/parts"
import { Pill } from "../agents/bits"
import type { Kind, KindField, NewSource, Run, SourceAction, SourceState } from "../data/types"

const KEY = ["integrations", "data"]

/** One word and one icon for where a source stands. */
function stateOf(s: Pick<SourceState, "status" | "publication">) {
  if (s.publication === "retired") return { label: "Retired", icon: Archive, tone: "text-muted-foreground" }
  if (s.status === "error") return { label: "Error", icon: CircleAlert, tone: "text-destructive" }
  if (s.status === "paused") return { label: "Paused", icon: CirclePause, tone: "text-muted-foreground" }
  if (s.publication === "published") return { label: "Published", icon: CircleCheck, tone: "text-success" }
  return { label: "Shadow", icon: EyeOff, tone: "text-muted-foreground" }
}

export function State({ s }: { s: Pick<SourceState, "status" | "publication"> }) {
  const { label, icon: Icon, tone } = stateOf(s)
  return <span className={`inline-flex items-center gap-1.5 ${tone}`}><Icon className="size-3.5" />{label}</span>
}

export function DataSources() {
  const q = useQuery({ queryKey: [...KEY, "sources"], queryFn: () => api.get<SourceState[]>("/api/integrations/data/sources") })
  const [, setSource] = useParam("source")
  const [adding, setAdding] = useState(false)
  return (
    <>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">Data sources</h2>
        <Button size="sm" onClick={() => setAdding(true)}><Plus />Add source</Button>
      </div>
      <QueryState query={q} empty="No data source yet">
        {(rows) => (
          <>
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Source</TableHead><TableHead>State</TableHead><TableHead className="text-right max-sm:hidden">Items</TableHead>
                  <TableHead className="max-md:hidden">Synced</TableHead><TableHead className="text-right max-md:hidden">Errors</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((s) => (
                  <TableRow key={s.id} {...rowProps(() => setSource(s.id))}>
                    <TableCell>
                      <span className="font-medium">{s.name}</span>
                      {s.bound && <Pill className="ml-2 gap-1"><Lock className="size-3" />Bound</Pill>}
                    </TableCell>
                    <TableCell><State s={s} /></TableCell>
                    <TableCell className="text-right tabular max-sm:hidden">{compact(s.records)}</TableCell>
                    <TableCell className="text-muted-foreground max-md:hidden"><Ago iso={s.last_sync_at} /></TableCell>
                    <TableCell className="text-right tabular max-md:hidden">{s.errors || ""}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Drawer param="source" title="Data source" ids={rows.map((r) => r.id)} wide>
              {(id) => { const s = rows.find((r) => r.id === id); return s ? <Detail s={s} /> : null }}
            </Drawer>
          </>
        )}
      </QueryState>
      {adding && <Add onClose={() => setAdding(false)} />}
    </>
  )
}

function Detail({ s }: { s: SourceState }) {
  const qc = useQueryClient()
  const confirm = useConfirm()
  const act = useMutation({
    mutationFn: (a: SourceAction) => api.post(`/api/integrations/data/sources/${s.id}/${a}`),
    onSuccess: (_r, a) => { void qc.invalidateQueries({ queryKey: KEY }); toast({ publish: "Published", unpublish: "Back to shadow", pause: "Paused", resume: "Resumed", retire: "Retired", sync: "Sync requested" }[a]) },
  })
  const runs = useQuery({ queryKey: [...KEY, "runs", s.id], queryFn: () => api.get<Run[]>(`/api/integrations/data/sources/${s.id}/runs`) })
  const locked = s.bound || act.isPending
  const retired = s.publication === "retired"
  const main = s.publication === "published"
    ? { label: "Unpublish", run: () => act.mutate("unpublish"), outline: true }
    : { label: "Publish", run: () => act.mutate("publish"), outline: false }
  const menu: MenuAction[] = retired ? [] : [
    { label: "Sync now", icon: <RefreshCw />, onSelect: () => act.mutate("sync"), disabled: locked },
    { label: s.status === "paused" ? "Resume" : "Pause", icon: <CirclePause />, onSelect: () => act.mutate(s.status === "paused" ? "resume" : "pause"), disabled: locked },
    { label: "Retire", icon: <Archive />, destructive: true, disabled: locked, onSelect: async () => {
      if (await confirm({ title: `Retire ${s.name}?`, body: "It stops syncing and is no longer read.", action: "Retire", destructive: true })) act.mutate("retire")
    } },
  ]
  return (
    <div>
      <PanelHead
        title={s.name} subtitle={s.kind} tags={s.bound ? <Pill className="gap-1"><Lock className="size-3" />Bound</Pill> : undefined}
        actions={!retired && <Button size="sm" variant={main.outline ? "outline" : "default"} disabled={locked || (!main.outline && !s.can_publish)} onClick={main.run}>{main.label}</Button>}
        menu={menu}
      />
      <PanelSection>
        <Facts rows={[
          ["State", <State key="s" s={s} />], ["Mode", s.mode === "stored" ? "Stored" : "Live"], ["Items", plural(s.records, "item")], ["Scope", s.default_scope], ["Group", s.group],
          ["Synced", s.last_sync_at ? <Ago key="a" iso={s.last_sync_at} /> : null], ["Publish", s.publication === "shadow" && !s.parity?.wrong_more ? s.blocked : null],
        ]} />
      </PanelSection>
      {s.parity && (
        <PanelSection title="Parity">
          <Facts rows={[
            ["Result", s.parity.ok ? <span key="r" className="inline-flex items-center gap-1.5"><CircleCheck className="size-3.5 text-success" />Passed</span> : <span key="r" className="inline-flex items-center gap-1.5"><CircleAlert className="size-3.5 text-destructive" />Not passed</span>],
            ["People", String(s.parity.people)], ["Checked", String(s.parity.checked)], ["Wider for", plural(s.parity.wrong_more, "person", "people")],
            ["Narrower for", plural(s.parity.wrong_less, "person", "people")], ["Unresolved", s.parity.unresolved],
          ]} />
        </PanelSection>
      )}
      <PanelSection title="Runs">
        <QueryState query={runs} empty="No run yet">
          {(rows) => (
            <ul className="divide-y rounded-lg border text-sm">
              {rows.map((r) => (
                <li key={r.id} className="px-3 py-2">
                  <div className="flex items-center gap-3">
                    <span className="inline-flex items-center gap-1.5">
                      {r.status === "failed" || r.status === "error" ? <CircleAlert className="size-3.5 text-destructive" /> : <CircleCheck className="size-3.5 text-success" />}
                      <span className="capitalize">{r.status}</span>
                    </span>
                    <span className="ml-auto text-xs text-muted-foreground"><Ago iso={r.started_at} /></span>
                  </div>
                  <dl className="mt-1 flex gap-4 text-xs text-muted-foreground tabular">
                    {([["Added", r.added], ["Changed", r.updated], ["Removed", r.deleted]] as const).map(([k, n]) => <div key={k} className="flex gap-1.5"><dt>{k}</dt><dd className="text-foreground">{n}</dd></div>)}
                  </dl>
                  {r.error && <div className="mt-1 break-words text-xs text-destructive">{r.error}</div>}
                </li>
              ))}
            </ul>
          )}
        </QueryState>
      </PanelSection>
    </div>
  )
}

function Field({ f, value, onChange }: { f: KindField; value: string; onChange: (v: string) => void }) {
  if (f.kind === "select") return <Cards label={f.label} value={value} onChange={onChange} choices={Object.fromEntries(f.options.map((o) => [o, o]))} />
  return (
    <Input
      id={`f-${f.name}`} autoComplete="off" type={f.kind === "secret" ? "password" : "text"} placeholder={f.placeholder}
      value={value} onChange={(e) => onChange(e.target.value)}
    />
  )
}

const valueOf = (f: KindField, v: string): string | string[] => (f.kind === "list" ? v.split(",").map((x) => x.trim()).filter(Boolean) : v.trim())

/** Add a source: its kind as cards, then that kind's own fields. */
export function Add({ onClose }: { onClose: () => void }) {
  const kinds = useQuery({ queryKey: [...KEY, "kinds"], queryFn: () => api.get<Kind[]>("/api/integrations/data/kinds") })
  const qc = useQueryClient()
  const [kind, setKind] = useState<string | null>(null)
  const [name, setName] = useState("")
  const [group, setGroup] = useState("")
  const [values, setValues] = useState<Record<string, string>>({})
  const picked = kinds.data?.find((k) => k.kind === (kind ?? kinds.data?.[0]?.kind))
  const create = useMutation({
    meta: { inline: true },
    mutationFn: (body: NewSource) => api.post("/api/integrations/data/sources", body),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: KEY }); toast("Source added"); onClose() },
  })
  const submit = () => {
    if (!picked) return
    const settings: NewSource["settings"] = {}
    for (const f of picked.fields) if (values[f.name]) settings[f.name] = valueOf(f, values[f.name]!)
    create.mutate({ kind: picked.kind, name: name.trim(), settings, group: group.trim() || null })
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>Add a data source</DialogTitle></DialogHeader>
        <QueryState query={kinds}>
          {(list) => (
            <form id="add-source" className="space-y-4" onSubmit={(e) => { e.preventDefault(); submit() }}>
              <div className="space-y-1.5">
                <Label>Kind</Label>
                <Cards label="Kind" value={picked?.kind ?? ""} onChange={(k) => { setKind(k); setValues({}) }} choices={Object.fromEntries(list.map((k) => [k.kind, k.name]))} className="max-sm:!grid-cols-2" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="source-name">Name</Label>
                <Input id="source-name" autoComplete="off" maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="source-group">Source group</Label>
                <Input id="source-group" autoComplete="off" maxLength={63} placeholder="google" value={group} onChange={(e) => setGroup(e.target.value)} />
              </div>
              {picked?.fields.map((f) => (
                <div key={f.name} className="space-y-1.5">
                  <Label htmlFor={`f-${f.name}`}>{f.label}</Label>
                  <Field f={f} value={values[f.name] ?? ""} onChange={(v) => setValues({ ...values, [f.name]: v })} />
                </div>
              ))}
            </form>
          )}
        </QueryState>
        <DialogFooter className="max-sm:flex-row">
          <Button variant="outline" className="max-sm:flex-1" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="add-source" className="max-sm:flex-1" disabled={!picked || !name.trim() || create.isPending}>Add source</Button>
        </DialogFooter>
        {create.error && <p role="alert" className="text-right text-sm text-destructive">{create.error.message}</p>}
      </DialogContent>
    </Dialog>
  )
}

import { X } from "lucide-react"
import { useState } from "react"

import { Avatar } from "@/components/common"
import { SettingRow } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useCatalog, useDraft, useDraftEditor, usePatch } from "./api"
import { clone } from "./spec"
import { ChoiceCards, EditStrip, Part } from "./tabparts"
import type { Catalog, Person, Workflow } from "./types"

const usd = (s: string) => { const n = Number(s.replace(/[^\d.]/g, "")); return s.trim() && Number.isFinite(n) ? n : null }

/** A number field that saves on blur, so typing never writes half a value. */
function Money({ label, value, disabled, onCommit }: { label: string; value: number | null; disabled?: boolean; onCommit: (n: number | null) => void }) {
  const [text, setText] = useState(value == null ? "" : String(value))
  return (
    <div className="relative max-w-40">
      <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm text-muted-foreground">$</span>
      <Input aria-label={label} inputMode="decimal" value={text} disabled={disabled} placeholder="None" className="h-8 pl-6 tabular"
        onChange={(e) => setText(e.target.value)} onBlur={() => { const n = usd(text); if (n !== value) onCommit(n) }}
        onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur() }} />
    </div>
  )
}

function PeopleList({ title, people, options, editable, onChange }: { title: string; people: Person[]; options: Person[]; editable: boolean; onChange: (ids: string[]) => void }) {
  const free = options.filter((p) => !people.some((x) => x.id === p.id))
  return (
    <SettingRow label={title} top>
      <div className="space-y-1">
        {people.map((p) => (
          <div key={p.id} className="flex h-8 items-center gap-2 text-sm">
            <Avatar name={p.name} /><span className="truncate">{p.name}</span>
            {editable && <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label={`Remove ${p.name}`} onClick={() => onChange(people.filter((x) => x.id !== p.id).map((x) => x.id))}><X /></Button>}
          </div>
        ))}
        {!people.length && <div className="flex h-8 items-center text-sm text-faint">None</div>}
        {editable && free.length > 0 && (
          <Select value="" onValueChange={(id) => onChange([...people.map((x) => x.id), id])}>
            <SelectTrigger aria-label={`Add to ${title}`} className="h-8 w-full max-w-60"><SelectValue placeholder="Add person" /></SelectTrigger>
            <SelectContent>{free.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}</SelectContent>
          </Select>
        )}
      </div>
    </SettingRow>
  )
}

/** Where templated `to`, `also` and `mention` values may land: channels by id, people as `person:<id>`. */
function Destinations({ value, catalog, editable, onChange }: { value: string[]; catalog: Catalog | undefined; editable: boolean; onChange: (v: string[]) => void }) {
  const all = [
    ...(catalog?.channels ?? []).map((c) => ({ id: c.id, name: `#${c.name}` })),
    ...(catalog?.people ?? []).map((p) => ({ id: `person:${p.id}`, name: p.name })),
  ]
  const name = (id: string) => all.find((d) => d.id === id)?.name ?? id
  const free = all.filter((d) => !value.includes(d.id))
  return (
    <SettingRow label="Can post to" top>
      <div className="space-y-1">
        {value.map((id) => (
          <div key={id} className="flex h-8 items-center gap-2 text-sm">
            <span className="truncate">{name(id)}</span>
            {editable && <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label={`Remove ${name(id)}`} onClick={() => onChange(value.filter((x) => x !== id))}><X /></Button>}
          </div>
        ))}
        {!value.length && <div className="flex h-8 items-center text-sm text-faint">None</div>}
        {editable && free.length > 0 && (
          <Select value="" onValueChange={(id) => onChange([...value, id])}>
            <SelectTrigger aria-label="Add a place to post to" className="h-8 w-full max-w-60"><SelectValue placeholder="Add channel or person" /></SelectTrigger>
            <SelectContent>{free.map((d) => <SelectItem key={d.id} value={d.id}>{d.name}</SelectItem>)}</SelectContent>
          </Select>
        )}
      </div>
    </SettingRow>
  )
}

export function AccessTab({ wf }: { wf: Workflow }) {
  const { data: draft } = useDraft(wf.id, wf.can_edit)
  const { data: catalog } = useCatalog()
  const editor = useDraftEditor(wf, draft)
  const patch = usePatch(wf.id)
  const spec = draft?.spec ?? wf.spec
  const edit = wf.can_edit
  const manage = wf.mine || wf.is_admin
  const set = (fn: (s: typeof spec) => void) => { const s = clone(spec); fn(s); editor.save(s, 0) }
  const people = catalog?.people ?? []
  const vis = spec.visibility ?? "private"
  const workspace = people.filter((p) => p.id !== wf.owner.id)
  return (
    <>
      <EditStrip wf={wf} />
      <div className="max-w-3xl px-4 py-6 lg:px-6">
        <Part title="Visibility">
          <ChoiceCards label="Visibility" value={vis} disabled={!edit} onChange={(v) => set((s) => { s.visibility = v })} options={[
            { value: "private", label: "Private", fact: "Owner and people listed here" },
            { value: "shared", label: "Shared", fact: "Everyone in the workspace can view" },
          ]} />
        </Part>
        <Part title="Ownership">
          <div className="space-y-2.5">
            <SettingRow label="Owner"><span className="flex h-8 items-center gap-2 text-sm"><Avatar name={wf.owner.name} />{wf.owner.name}</span></SettingRow>
            <SettingRow label="Runs as"><span className="flex h-8 items-center gap-2 text-sm">{wf.run_as ? <><Avatar name={wf.run_as.name} />{wf.run_as.name}</> : <span className="text-muted-foreground">Needs an owner</span>}</span></SettingRow>
          </div>
        </Part>
        <Part title="People">
          <div className="space-y-3">
            <PeopleList title="Editors" people={wf.editors} options={workspace} editable={manage} onChange={(ids) => patch.mutate({ editors: ids })} />
            <PeopleList title="Runners" people={wf.runners} options={workspace} editable={manage} onChange={(ids) => patch.mutate({ runners: ids })} />
            <PeopleList title="Viewers" people={wf.viewers} options={[]} editable={false} onChange={() => {}} />
          </div>
        </Part>
        <Part title="Limits">
          <div className="space-y-2.5">
            <SettingRow label="Home channel">
              <Select value={spec.home?.channel ?? ""} disabled={!edit} onValueChange={(id) => set((s) => { s.home = { channel: id } })}>
                <SelectTrigger aria-label="Home channel" className="h-8 w-full max-w-60"><SelectValue placeholder="None" /></SelectTrigger>
                <SelectContent>{(catalog?.channels ?? []).map((c) => <SelectItem key={c.id} value={c.id}>#{c.name}</SelectItem>)}</SelectContent>
              </Select>
            </SettingRow>
            <Destinations value={spec.destinations ?? []} catalog={catalog} editable={edit} onChange={(v) => set((s) => { s.destinations = v.length ? v : undefined })} />
            <SettingRow label="Run budget"><Money key={String(spec.limits?.budget_usd)} label="Run budget" value={spec.limits?.budget_usd ?? null} disabled={!edit} onCommit={(n) => set((s) => { s.limits = { ...s.limits, budget_usd: n ?? undefined } })} /></SettingRow>
            <SettingRow label="Monthly cap"><Money key={String(wf.monthly_cap_usd)} label="Monthly cap" value={wf.monthly_cap_usd} disabled={!manage} onCommit={(n) => patch.mutate({ monthly_cap_usd: n })} /></SettingRow>
          </div>
        </Part>
      </div>
    </>
  )
}

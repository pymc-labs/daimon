import { Bot, Building2, Hash, Lock, UserRound } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { CreditMeter } from "@/components/CreditMeter"
import { Drawer } from "@/components/Drawer"
import { Empty } from "@/components/filters"
import { PanelHead, PanelSection, SettingRow, Tag } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useConfirm } from "@/lib/confirm"
import { shortDate } from "@/lib/format"
import { rowProps, useParam } from "@/lib/filters"
import { Field } from "@/modules/admin/shared"
import { SubjectPicker } from "@/modules/credits/Allowances"
import type { Subject } from "@/modules/credits/types"
import { usePlaces, useSupportSave, type Outcome } from "./api"
import { emptyForm, formOf, limitErrors, toValue, type LimitErrors, type LimitForm } from "./limitForm"
import { credits, keyOf, placeName } from "./lib"
import { Cards, Section } from "./parts"
import type { Limit, Place, Scope, SupportPage, Window } from "./types"

const ICONS = { workspace: Building2, agent: Bot, channel: Hash, person: UserRound }
const KIND = { workspace: "Workspace", agent: "Agent", channel: "Channel", person: "Person" }
const STATES = { not_started: "Not started", ended: "Ended" } as const
const RESETS: Record<Window, string> = { total: "Never", monthly: "Every month" }
const RANK: Record<Scope, number> = { workspace: 0, agent: 1, channel: 2 }

/** Places first, general to specific, then each person's row. */
const ordered = (list: Limit[]) =>
  [...list].sort((a, b) => Number(!!a.person_id) - Number(!!b.person_id) || RANK[a.scope] - RANK[b.scope] || (a.name ?? "").localeCompare(b.name ?? "") || (a.person_name ?? "").localeCompare(b.person_name ?? ""))

const kindOf = (l: Limit) => (l.person_id ? "person" : l.scope)
const each = (l: Limit) => (l.each_person == null ? "–" : `${credits(Number(l.each_person))}${l.window === "monthly" ? " / month" : " total"}`)
const shared = (l: Limit) => (l.shared == null ? "–" : `${credits(Number(l.shared))}${l.window === "monthly" ? " / month" : " total"}`)
const until = (l: Limit) => (l.ends_at ? shortDate(new Date(Date.parse(l.ends_at) - 1).toISOString()) : "Never")

/** The credits a row is measured against: the shared part, or a person's own. */
const cap = (l: Limit) => (l.person_id ? l.each_person : l.shared)

function Name({ l }: { l: Limit }) {
  const kind = kindOf(l)
  const Icon = ICONS[kind]
  return (
    <span className="flex min-w-0 items-center gap-2.5">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-accent"><Icon className="size-3.5 text-muted-foreground" /></span>
      <span className="truncate font-medium">{l.person_id ? (l.person_name ?? "Unknown person") : placeName(l)}</span>
      {(l.person_id || l.scope !== "workspace") && <span className="min-w-0 truncate text-xs text-muted-foreground max-md:hidden">{l.person_id ? (l.scope === "workspace" ? "Person" : `Person in ${l.name}`) : KIND[kind]}</span>}
      {l.state !== "active" && <Tag>{STATES[l.state]}</Tag>}
    </span>
  )
}

export function Used({ l }: { l: Limit }) {
  const total = cap(l)
  if (total == null) return <span className="text-muted-foreground tabular">{credits(Number(l.used))} used</span>
  return <CreditMeter compact format={(n) => credits(n)} meter={{ left_usd: Number(total) - Number(l.used), total_usd: Number(total), window: l.window === "monthly" ? "month" : "total", resets_at: null }} />
}

/** Step 4: one list of limits, general to specific. */
export function Limits({ d }: { d: SupportPage }) {
  const [adding, setAdding] = useState(false)
  const [, setOpen] = useParam("limit")
  const [add, setAdd] = useParam("add")
  const list = ordered(d.limits)
  return (
    <Section title="Credits" add={<Button size="xs" variant="outline" onClick={() => setAdding(true)}>Add limit</Button>}>
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead>For</TableHead><TableHead className="text-right max-md:hidden">Shared</TableHead><TableHead className="text-right">Each person</TableHead>
            <TableHead className="w-44 max-sm:hidden">Used</TableHead><TableHead className="max-lg:hidden">Until</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow className="hover:bg-transparent">
            <TableCell>
              <span className="flex items-center gap-2.5">
                <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-accent"><UserRound className="size-3.5 text-muted-foreground" /></span>
                <span className="font-medium">Everyone</span><span className="text-xs text-muted-foreground">Built in</span>
              </span>
            </TableCell>
            <TableCell className="text-right text-muted-foreground max-md:hidden">–</TableCell>
            <TableCell className="text-right whitespace-nowrap tabular">{credits(Number(d.everyone.each_person))}<span className="text-muted-foreground"> total</span></TableCell>
            <TableCell className="max-sm:hidden"><span className="inline-flex items-center gap-1.5 text-muted-foreground"><Lock className="size-3.5" />Locked</span></TableCell>
            <TableCell className="text-muted-foreground max-lg:hidden">Never</TableCell>
          </TableRow>
          {list.map((l) => (
            <TableRow key={keyOf(l)} {...rowProps(() => setOpen(keyOf(l)))}>
              <TableCell className="max-md:max-w-0"><Name l={l} /></TableCell>
              <TableCell className="text-right whitespace-nowrap tabular max-md:hidden">{shared(l)}</TableCell>
              <TableCell className="text-right whitespace-nowrap tabular">{each(l)}</TableCell>
              <TableCell className="max-sm:hidden"><Used l={l} /></TableCell>
              <TableCell className="text-muted-foreground max-lg:hidden">{until(l)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {!list.length && <Empty className="py-6">No limits</Empty>}
      <Drawer param="limit" title="Limit" ids={list.map(keyOf)}>
        {(id) => {
          const l = list.find((x) => keyOf(x) === id)
          return l ? <LimitDetail key={id} l={l} /> : null
        }}
      </Drawer>
      <AddLimit key={add ?? ""} d={d} open={adding || !!add} onOpenChange={(o) => { setAdding(o); if (!o) setAdd(null) }} channel={add ?? ""} />
    </Section>
  )
}

/** Cancel, then Save rightmost; each takes an equal share of the row on a phone. */
export function Actions({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={`flex justify-end gap-2 [&>button]:max-sm:flex-1 ${className ?? ""}`}>{children}</div>
}

export const Failure = ({ children }: { children: React.ReactNode }) => (children ? <p role="alert" className="mt-2 text-xs text-destructive">{children}</p> : null)

/** The inputs of a limit; a person's row has no shared part. */
function Fields({ form, set, person, errors }: { form: LimitForm; set: (f: Partial<LimitForm>) => void; person: boolean; errors: LimitErrors["at"] }) {
  const input = (label: string, key: "shared" | "each", err?: string) => (
    <Field label={label}>
      <Input aria-label={label} aria-invalid={!!err} inputMode="decimal" className="h-8" value={form[key]} onChange={(e) => set({ [key]: e.target.value.replace(/[^\d.]/g, "") })} />
      {err && <p className="text-xs text-destructive">{err}</p>}
    </Field>
  )
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
        {!person && input("Shared", "shared", errors.shared)}
        {input(person ? "Credits" : "Each person", "each", errors.each_person)}
      </div>
      <Field label="Resets"><Cards label="Resets" value={form.window} onChange={(window) => set({ window })} choices={RESETS} /></Field>
      <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
        <Field label="From"><Input aria-label="From" type="date" className="h-8" value={form.from} onChange={(e) => set({ from: e.target.value })} />{errors.starts_at && <p className="text-xs text-destructive">{errors.starts_at}</p>}</Field>
        <Field label="Last day"><Input aria-label="Last day" type="date" className="h-8" value={form.last} onChange={(e) => set({ last: e.target.value })} />{errors.ends_at && <p className="text-xs text-destructive">{errors.ends_at}</p>}</Field>
      </div>
    </div>
  )
}

const FOR: Record<"workspace" | "agent" | "channel" | "person", string> = KIND

/** Add limit: who it is for, the place, then the credits and dates. */
function AddLimit({ d, open, onOpenChange, channel }: { d: SupportPage; open: boolean; onOpenChange: (o: boolean) => void; channel: string }) {
  const save = useSupportSave()
  const places = usePlaces(d)
  const [kind, setKind] = useState<keyof typeof FOR>("channel")
  const [place, setPlace] = useState(channel)
  const [person, setPerson] = useState<Subject | null>(null)
  const [where, setWhere] = useState("everywhere")
  const [form, setForm] = useState<LimitForm>(emptyForm)
  const [out, setOut] = useState<Outcome | null>(null)
  const has = (scope: Scope, id: string, who: string | null) => d.limits.some((l) => l.scope === scope && l.scope_id === id && l.person_id === who)
  const free = places.filter((p) => p.scope === kind && !has(p.scope, p.id, null))
  const isPerson = kind === "person"
  const at = isPerson ? (where === "everywhere" ? { scope: "workspace" as Scope, scope_id: "" } : { scope: where.split("|")[0] as Scope, scope_id: where.split("|")[1] }) : kind === "workspace" ? { scope: "workspace" as Scope, scope_id: "" } : { scope: kind as Scope, scope_id: place }
  const ready = isPerson ? !!person && !!form.each.trim() : kind === "workspace" ? true : !!place
  const errors = limitErrors(out?.error ?? null, out?.problems ?? [])
  const reset = () => { setKind("channel"); setPlace(""); setPerson(null); setWhere("everywhere"); setForm(emptyForm()); setOut(null) }
  const submit = async () => {
    const r = await save.run("limits", { ...at, person_id: isPerson ? person!.id : null, limit: toValue(form, null, isPerson), expected_revision: null })
    setOut(r)
    if (r.error) return
    onOpenChange(false); reset(); toast("Limit added")
  }
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) reset() }}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader><DialogTitle>Add limit</DialogTitle></DialogHeader>
        <div className="space-y-4">
          <Field label="For">
            <Cards label="For" value={kind} onChange={(k) => { setKind(k); setPlace(""); setOut(null) }} choices={FOR} disabled={has("workspace", "", null) ? ["workspace"] : []} className="max-sm:!grid-cols-2" />
          </Field>
          {(kind === "agent" || kind === "channel") && (
            <Field label={FOR[kind]}>
              <Select value={place} onValueChange={setPlace}>
                <SelectTrigger className="w-full" aria-label={FOR[kind]}><SelectValue placeholder="Pick one" /></SelectTrigger>
                <SelectContent>{free.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}</SelectContent>
              </Select>
            </Field>
          )}
          {isPerson && (
            <>
              <Field label="Person"><SubjectPicker peopleOnly value={person} onChange={setPerson} /></Field>
              <Field label="In">
                <Select value={where} onValueChange={setWhere}>
                  <SelectTrigger className="w-full" aria-label="In"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="everywhere">Everywhere</SelectItem>
                    {(["agent", "channel"] as const).map((s) => (
                      <SelectGroup key={s}>
                        <SelectLabel>{s === "agent" ? "Agents" : "Channels"}</SelectLabel>
                        {places.filter((p: Place) => p.scope === s).map((p) => <SelectItem key={p.id} value={`${s}|${p.id}`}>{p.name}</SelectItem>)}
                      </SelectGroup>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            </>
          )}
          <Fields form={form} set={(f) => setForm((x) => ({ ...x, ...f }))} person={isPerson} errors={errors.at} />
        </div>
        <div>
          <Actions>
            <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button disabled={!ready || save.pending} onClick={() => void submit()}>Add limit</Button>
          </Actions>
          <Failure>{errors.rest}</Failure>
        </div>
      </DialogContent>
    </Dialog>
  )
}

const money = (n: string) => credits(Number(n))

/** A limit in a drawer: its inputs, what it has used, the latest charges. */
function LimitDetail({ l }: { l: Limit }) {
  const save = useSupportSave()
  const confirm = useConfirm()
  const [, setOpen] = useParam("limit")
  const person = !!l.person_id
  // The limit as it was read: what a save expects, whatever has been read since.
  const [seen, setSeen] = useState<Limit>(l)
  const [form, setForm] = useState<LimitForm>(() => formOf(l))
  const [out, setOut] = useState<Outcome | null>(null)
  const errors = limitErrors(out?.error ?? null, out?.problems ?? [])
  const dirty = JSON.stringify(form) !== JSON.stringify(formOf(seen))
  const Icon = ICONS[kindOf(l)]
  const send = async (limit: ReturnType<typeof toValue> | null) => {
    const r = await save.run("limits", { scope: l.scope, scope_id: l.scope_id, person_id: l.person_id, limit, expected_revision: seen.revision })
    setOut(r)
    if (!r.error) { setOpen(null); toast(limit ? "Limit saved" : "Limit removed"); return }
    if (!r.fresh) return
    const now = r.fresh.limits.find((x) => keyOf(x) === keyOf(l))
    if (now) { setSeen(now); setForm(formOf(now)) } else setOpen(null)
  }
  const remove = async () => {
    if (await confirm({ title: "Remove limit?", body: person ? l.person_name ?? undefined : placeName(l), action: "Remove", destructive: true })) await send(null)
  }
  return (
    <div className="flex min-h-full flex-col">
      <PanelHead
        icon={<Icon className="size-4 text-muted-foreground" />}
        title={person ? (l.person_name ?? "Unknown person") : placeName(l)}
        subtitle={person ? (l.scope === "workspace" ? "Person, everywhere" : `Person in ${l.name}`) : KIND[kindOf(l)]}
        tags={l.state !== "active" ? <Tag>{STATES[l.state]}</Tag> : undefined}
      />
      <PanelSection title="Credits">
        <div className="space-y-4">
          <SettingRow label="Used">{cap(l) == null ? <span className="tabular">{money(l.used)}</span> : <Used l={l} />}</SettingRow>
          <Fields form={form} set={(f) => setForm((x) => ({ ...x, ...f }))} person={person} errors={errors.at} />
        </div>
      </PanelSection>
      {l.recent.length > 0 && (
        <PanelSection title="Recent charges">
          <ul className="space-y-1.5 text-sm">
            {l.recent.map((c, i) => (
              <li key={i} className="grid grid-cols-[1fr_auto] gap-3">
                <span className="min-w-0 truncate">{c.person}<span className="text-muted-foreground"> {c.what}</span></span>
                <span className="tabular text-muted-foreground">{money(c.credits)}</span>
              </li>
            ))}
          </ul>
        </PanelSection>
      )}
      <div className="sticky bottom-0 mt-auto border-t bg-popover px-5 py-3">
        <Actions>
          <Button variant="ghost" className="mr-auto text-destructive max-sm:mr-0" disabled={save.pending} onClick={() => void remove()}>Remove</Button>
          <Button variant="outline" onClick={() => setOpen(null)}>Cancel</Button>
          <Button disabled={!dirty || save.pending} onClick={() => void send(toValue(form, seen, person))}>Save</Button>
        </Actions>
        <Failure>{errors.rest}</Failure>
      </div>
    </div>
  )
}

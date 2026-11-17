import { useState } from "react"

import { Mono } from "@/components/common"
import { Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useConfirm } from "@/lib/confirm"
import { channelLabel, plural } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Field } from "@/modules/admin/shared"
import { useChannels, useSupportPlans, useSupportSave, type Outcome, type SupportOutcome } from "./api"
import { MenuEditor } from "./MenuEditor"
import { cleanKey, emptyMenu, menuErrors, toCatalog, toDraft, type MenuDraft } from "./menuDraft"
import { Notice } from "./shared"
import type { PersonCap, PlanScope, PlanValue, PlanWindow, SupportMenu, SupportPlan, SupportPlans } from "./types"

const SCOPES: Record<PlanScope, string> = { workspace: "Workspace", agent: "Agent", channel: "Channel" }
const WINDOWS: Record<PlanWindow, string> = { total: "In total", monthly: "Each month", fixed: "Between dates" }
const STATES = { not_started: "Not started", ended: "Ended" } as const
/** The select value for the default menu (a plan with no `catalog`). */
const DEFAULT = "default"

const day = (iso: string | null) => (iso ? iso.slice(0, 10) : "")
const keyOf = (p: { scope: PlanScope; scope_id: string }) => `${p.scope}:${p.scope_id}`
const placeOf = (p: SupportPlan) => p.scope === "workspace" ? "Workspace" : p.scope === "channel" ? channelLabel(p.name ?? p.scope_id) : (p.name ?? p.scope_id)
const valueOf = ({ scope, scope_id, catalog, pool, per_person, window, starts_at, ends_at }: SupportPlan): PlanValue =>
  ({ scope, scope_id, catalog, pool, per_person, window, starts_at, ends_at })

/** Plans, own caps and menus for Ask a person; each save answers with the whole page. */
export function SupportTab() {
  const query = useSupportPlans()
  return (
    <QueryState query={query} className="px-4 pt-6 lg:px-6">{(d: SupportPlans) => (
      <div className="space-y-8 px-4 py-6 lg:px-6">
        <Plans d={d} />
        <Caps d={d} />
        <Menus d={d} />
      </div>
    )}</QueryState>
  )
}

function Section({ title, add, children }: { title: string; add?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="mx-auto w-full max-w-3xl">
      <div className="mb-2 flex h-8 items-center justify-between gap-3"><h2 className="text-sm font-semibold">{title}</h2>{add}</div>
      <div className="divide-y rounded-lg border text-sm">{children}</div>
    </section>
  )
}

const Line = ({ children }: { children: React.ReactNode }) => <div className="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1 py-1.5">{children}</div>
const Empty = ({ children }: { children: React.ReactNode }) => <div className="px-4 py-3 text-muted-foreground">{children}</div>

/** Save rightmost, Cancel and Remove before it; each takes the full row on a phone. */
function Actions({ pending, ready, onSave, onCancel, onRemove }: { pending: boolean; ready: boolean; onSave: () => void; onCancel: () => void; onRemove?: () => void }) {
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {onRemove && <Button size="sm" variant="ghost" className="text-destructive max-sm:flex-1" disabled={pending} onClick={onRemove}>Remove</Button>}
      <Button size="sm" variant="ghost" className="max-sm:flex-1" onClick={onCancel}>Cancel</Button>
      <Button size="sm" className="max-sm:flex-1" disabled={pending || !ready} onClick={onSave}>Save</Button>
    </div>
  )
}

function Plans({ d }: { d: SupportPlans }) {
  const [open, setOpen] = useState<string | null>(null)
  const menus = new Map(d.catalogs.map((m) => [m.key, m.name || m.key]))
  return (
    <Section title="Plans" add={open !== "new" && <Button size="xs" variant="outline" onClick={() => setOpen("new")}>Add plan</Button>}>
      {open === "new" && <div className="px-4 py-4"><PlanForm d={d} onDone={() => setOpen(null)} /></div>}
      {d.plans.map((p) => (
        <div key={keyOf(p)} className="px-4">
          <Line>
            <span className="font-medium">{placeOf(p)}</span>
            <span className="min-w-0 flex-1 text-muted-foreground">{p.summary}</span>
            {p.catalog && <Tag>{menus.get(p.catalog) ?? p.catalog}</Tag>}
            {p.state !== "active" && <Tag>{STATES[p.state]}</Tag>}
            {open !== keyOf(p) && <Button size="xs" variant="outline" onClick={() => setOpen(keyOf(p))}>Edit</Button>}
          </Line>
          {open === keyOf(p) && <div className="border-t py-4"><PlanForm d={d} plan={p} onDone={() => setOpen(null)} /></div>}
        </div>
      ))}
      {!d.plans.length && open !== "new" && <Empty>No plans</Empty>}
    </Section>
  )
}

function PlanForm({ d, plan, onDone }: { d: SupportPlans; plan?: SupportPlan; onDone: () => void }) {
  const save = useSupportSave()
  const confirm = useConfirm()
  const { data: list } = useChannels()
  const taken = new Set(d.plans.map(keyOf))
  const free = (scope: PlanScope, id: string) => !!plan || !taken.has(keyOf({ scope, scope_id: id }))
  const [out, setOut] = useState<Outcome | null>(null)
  // The plan as it was when the form opened: what a save expects, whatever has been read since.
  const [expected, setExpected] = useState<PlanValue | null>(plan ? valueOf(plan) : null)
  const [scope, setScope] = useState<PlanScope>(plan?.scope ?? (free("workspace", "") ? "workspace" : "channel"))
  const [place, setPlace] = useState(plan?.scope_id ?? "")
  const [menu, setMenu] = useState(plan?.catalog ?? DEFAULT)
  const [pool, setPool] = useState(plan?.pool ?? "")
  const [each, setEach] = useState(plan?.per_person ?? "")
  const [window, setWindow] = useState<PlanWindow>(plan?.window ?? "total")
  const [starts, setStarts] = useState(day(plan?.starts_at ?? null))
  const [ends, setEnds] = useState(day(plan?.ends_at ?? null))
  // Keep the instant as read when its day is untouched.
  const instant = (v: string, was: string | null) => (!v ? null : v === day(was) ? was : v)
  const channels = (list?.channels ?? []).filter((c) => c.kind === "channel" && c.platform === d.platform)
  const choices = (scope === "agent" ? d.agents : channels.map((c) => ({ id: c.id, name: channelLabel(c.name, c.kind) })))
    .filter((c) => free(scope, c.id))
  const target = scope === "workspace" ? "" : place
  /** Fill the form from a plan as the server has it now, and expect that value. */
  const load = (p: SupportPlan) => {
    setExpected(valueOf(p)); setMenu(p.catalog ?? DEFAULT); setPool(p.pool ?? ""); setEach(p.per_person ?? "")
    setWindow(p.window); setStarts(day(p.starts_at)); setEnds(day(p.ends_at))
  }
  const finish = (r: SupportOutcome) => {
    setOut(r)
    if (!r.error) return onDone()
    if (!r.fresh) return
    const now = r.fresh.plans.find((p) => keyOf(p) === keyOf({ scope, scope_id: target }))
    if (now) load(now)
    else if (plan) onDone()
  }

  const submit = async () => finish(await save.run("plan", {
    scope, scope_id: target, expected,
    plan: {
      catalog: menu === DEFAULT ? null : menu, pool: pool || null, per_person: each || null, window,
      starts_at: window === "monthly" ? null : instant(starts, expected?.starts_at ?? null),
      ends_at: window === "fixed" ? instant(ends, expected?.ends_at ?? null) : null,
    },
  }))
  const remove = async () => {
    if (!plan) return
    const caps = plan.allowances.length
    if (!(await confirm({ title: "Remove the plan?", body: caps ? `${plural(caps, "own cap")} go with it.` : undefined, action: "Remove", destructive: true }))) return
    finish(await save.run("plan", { scope: plan.scope, scope_id: plan.scope_id, plan: null, expected }))
  }

  return (
    <div className="space-y-3">
      {!plan && (
        <div role="radiogroup" aria-label="Applies to" className="grid grid-cols-3 gap-2">
          {(Object.keys(SCOPES) as PlanScope[]).map((s) => (
            <button
              key={s} type="button" role="radio" aria-checked={scope === s} disabled={s === "workspace" && !free(s, "")}
              onClick={() => { setScope(s); setPlace("") }}
              className={cn("rounded-lg border px-3 py-3 text-left font-medium transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50", scope === s && "border-foreground/40 bg-accent")}
            >{SCOPES[s]}</button>
          ))}
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
        {!plan && scope !== "workspace" && (
          <Field label={SCOPES[scope]}>
            <Select value={place} onValueChange={setPlace}>
              <SelectTrigger className="w-full" aria-label={SCOPES[scope]}><SelectValue placeholder="Pick one" /></SelectTrigger>
              <SelectContent>{choices.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}</SelectContent>
            </Select>
          </Field>
        )}
        <Field label="Menu">
          <Select value={menu} onValueChange={setMenu}>
            <SelectTrigger className="w-full" aria-label="Menu"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT}>Default</SelectItem>
              {d.catalogs.map((m) => <SelectItem key={m.key} value={m.key}>{m.name || m.key}</SelectItem>)}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Shared credits"><Input aria-label="Shared credits" inputMode="decimal" value={pool} onChange={(e) => setPool(e.target.value)} placeholder="10" /></Field>
        <Field label="Credits per person"><Input aria-label="Credits per person" inputMode="decimal" value={each} onChange={(e) => setEach(e.target.value)} placeholder="2" /></Field>
        <Field label="Window">
          <Select value={window} onValueChange={(v) => setWindow(v as PlanWindow)}>
            <SelectTrigger className="w-full" aria-label="Window"><SelectValue /></SelectTrigger>
            <SelectContent>{(Object.keys(WINDOWS) as PlanWindow[]).map((w) => <SelectItem key={w} value={w}>{WINDOWS[w]}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
        {window !== "monthly" && <Field label={`${window === "fixed" ? "From" : "Counting from"} (UTC)`}><Input aria-label={`${window === "fixed" ? "From" : "Counting from"} (UTC)`} type="date" value={starts} onChange={(e) => setStarts(e.target.value)} /></Field>}
        {window === "fixed" && <Field label="Until (UTC)"><Input aria-label="Until (UTC)" type="date" value={ends} onChange={(e) => setEnds(e.target.value)} /></Field>}
      </div>
      <Actions pending={save.pending} ready={!!plan || scope === "workspace" || !!place} onSave={() => void submit()} onCancel={onDone} onRemove={plan && (() => void remove())} />
      <Notice out={out} />
    </div>
  )
}

type CapRow = { cap: PersonCap; plan: SupportPlan | null }
const capRows = (d: SupportPlans): CapRow[] => [
  ...d.default_allowances.map((cap) => ({ cap, plan: null })),
  ...d.plans.flatMap((plan) => plan.allowances.map((cap) => ({ cap, plan }))),
]
const capAt = (rows: CapRow[], where: string, id: string) =>
  rows.find((r) => (r.plan ? keyOf(r.plan) : DEFAULT) === where && r.cap.platform_user_id === id)?.cap.credits ?? null
const capPlace = (plan: SupportPlan | null) => (plan ? { scope: plan.scope, scope_id: plan.scope_id } : {})

function Caps({ d }: { d: SupportPlans }) {
  const save = useSupportSave()
  const [out, setOut] = useState<Outcome | null>(null)
  const [adding, setAdding] = useState(false)
  const rows = capRows(d)
  const clear = async ({ cap, plan }: CapRow) =>
    setOut(await save.run("allowances", { platform_user_id: cap.platform_user_id, credits: null, ...capPlace(plan), expected: cap.credits }))
  return (
    <Section title="Own caps" add={!adding && <Button size="xs" variant="outline" onClick={() => setAdding(true)}>Add cap</Button>}>
      <div className="px-4"><Line><span className="flex-1">Default allowance</span><span className="tabular text-muted-foreground">{plural(Number(d.default_credits), "credit")} per person</span></Line></div>
      {adding && <div className="px-4 py-4"><CapForm d={d} rows={rows} onDone={() => setAdding(false)} /></div>}
      {rows.map((r) => (
        <div key={`${r.plan ? keyOf(r.plan) : DEFAULT}/${r.cap.platform_user_id}`} className="px-4">
          <Line>
            <span className="min-w-0 flex-1 truncate">{r.cap.name ?? <Mono>{r.cap.platform_user_id}</Mono>}</span>
            <span className="text-muted-foreground">{r.plan ? placeOf(r.plan) : "No plan"}</span>
            <span className="w-24 text-right tabular">{plural(Number(r.cap.credits), "credit")}</span>
            <Button size="xs" variant="ghost" aria-label={`Clear ${r.cap.name ?? r.cap.platform_user_id}`} disabled={save.pending} onClick={() => void clear(r)}>Clear</Button>
          </Line>
        </div>
      ))}
      {out?.error && <div className="px-4 pb-3"><Notice out={out} /></div>}
    </Section>
  )
}

function CapForm({ d, rows, onDone }: { d: SupportPlans; rows: CapRow[]; onDone: () => void }) {
  const save = useSupportSave()
  const [out, setOut] = useState<Outcome | null>(null)
  const [person, setPerson] = useState("")
  const [where, setWhere] = useState(DEFAULT)
  const [credits, setCredits] = useState("")
  // The caps as they were when the form opened: what a save expects, whatever has been read since.
  const [seen, setSeen] = useState(rows)
  const plan = d.plans.find((p) => keyOf(p) === where) ?? null
  const submit = async () => {
    const id = person.trim()
    const r = await save.run("allowances", { platform_user_id: id, credits, ...capPlace(plan), expected: capAt(seen, where, id) })
    setOut(r)
    if (!r.error) return onDone()
    if (!r.fresh) return
    const now = capRows(r.fresh)
    setSeen(now)
    setCredits(capAt(now, where, id) ?? "")
  }
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-3 max-sm:grid-cols-1">
        <Field label="User id"><Input aria-label="User id" value={person} onChange={(e) => setPerson(e.target.value)} /></Field>
        <Field label="Plan">
          <Select value={where} onValueChange={setWhere}>
            <SelectTrigger className="w-full" aria-label="Plan"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT}>No plan</SelectItem>
              {d.plans.map((p) => <SelectItem key={keyOf(p)} value={keyOf(p)}>{placeOf(p)}</SelectItem>)}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Credits"><Input aria-label="Credits" inputMode="decimal" value={credits} onChange={(e) => setCredits(e.target.value)} placeholder="2" /></Field>
      </div>
      <Actions pending={save.pending} ready={!!person.trim() && !!credits} onSave={() => void submit()} onCancel={onDone} />
      <Notice out={out} />
    </div>
  )
}

function Menus({ d }: { d: SupportPlans }) {
  const [open, setOpen] = useState<string | null>(null)
  const used = new Set(d.plans.map((p) => p.catalog))
  return (
    <Section title="Menus" add={open !== "" && <Button size="xs" variant="outline" onClick={() => setOpen("")}>Add menu</Button>}>
      {open === "" && <div className="px-4 py-4"><MenuForm onDone={() => setOpen(null)} /></div>}
      {d.catalogs.map((m) => (
        <div key={m.key} className="px-4">
          <Line>
            <span className="font-medium">{m.name || m.key}</span>
            <span className="min-w-0 flex-1 text-muted-foreground">{m.summary}</span>
            {open !== m.key && <Button size="xs" variant="outline" onClick={() => setOpen(m.key)}>Edit</Button>}
          </Line>
          {open === m.key && <div className="border-t py-4"><MenuForm menu={m} used={used.has(m.key)} onDone={() => setOpen(null)} /></div>}
        </div>
      ))}
      {!d.catalogs.length && open !== "" && <Empty>No menus</Empty>}
    </Section>
  )
}

/** The ids of every row in order: it changes when a row is added, removed or moved. */
const shape = (m: MenuDraft) => JSON.stringify(m.options.map((o) => [o.id, o.fields.map((f) => f.id), o.price.steps.map((x) => x.id)]))

function MenuForm({ menu, used, onDone }: { menu?: SupportMenu; used?: boolean; onDone: () => void }) {
  const save = useSupportSave()
  const confirm = useConfirm()
  const [out, setOut] = useState<Outcome | null>(null)
  const [key, setKey] = useState(menu?.key ?? "")
  const [draft, setDraft] = useState(() => (menu ? toDraft(menu.catalog) : emptyMenu()))
  // The revision as it was when the form opened: what a save expects, whatever has been read since.
  const [revision, setRevision] = useState(menu?.revision ?? null)
  const errors = menuErrors(out?.error ?? null, draft)
  // An error names rows by place, so it goes when the rows move.
  const edit = (next: MenuDraft) => { if (shape(next) !== shape(draft)) setOut(null); setDraft(next) }
  const finish = (r: SupportOutcome) => {
    setOut(r)
    if (!r.error) return onDone()
    if (!r.fresh) return
    const now = r.fresh.catalogs.find((m) => m.key === key.trim())
    if (now) { setDraft(toDraft(now.catalog)); setRevision(now.revision) }
    else if (menu) onDone()
  }
  const put = (catalog: Record<string, unknown> | null) =>
    save.run(`catalogs/${encodeURIComponent(key.trim())}`, { catalog, expected_revision: revision })
  const remove = async () => {
    if (!(await confirm({ title: "Remove the menu?", action: "Remove", destructive: true }))) return
    finish(await put(null))
  }
  return (
    <div className="space-y-4">
      {!menu && <Field label="Key"><Input aria-label="Key" className="w-60 max-sm:w-full" maxLength={32} value={key} onChange={(e) => setKey(cleanKey(e.target.value, "-"))} placeholder="support" /></Field>}
      <MenuEditor draft={draft} errors={errors} onChange={edit} />
      <div>
        <Actions pending={save.pending} ready={!!key.trim()} onSave={() => void put(toCatalog(draft)).then(finish)} onCancel={onDone} onRemove={menu && !used ? () => void remove() : undefined} />
        <Notice out={out && { ...out, error: errors.rest }} />
      </div>
    </div>
  )
}

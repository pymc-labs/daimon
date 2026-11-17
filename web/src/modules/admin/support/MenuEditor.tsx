import { ArrowDown, ArrowUp, ChevronDown, X } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { Field } from "@/modules/admin/shared"
import {
  FIELDS, LIMITS, move, newField, newOption, newStep, PRICES, used,
  type FieldDraft, type FieldKind, type MenuDraft, type MenuErrors, type OptionDraft, type PriceKind, type StepDraft,
} from "./menuDraft"

const Err = ({ children }: { children?: string }) => (children ? <p className="text-xs text-destructive">{children}</p> : null)

type TxtProps = Omit<React.ComponentProps<typeof Input>, "onChange"> & { aria: string; error?: string; onChange: (v: string) => void }
const Txt = ({ aria, error, onChange, ...props }: TxtProps) => (
  <div className="min-w-0 space-y-1">
    <Input aria-label={aria} aria-invalid={!!error} onChange={(e) => onChange(e.target.value)} {...props} />
    <Err>{error}</Err>
  </div>
)
const Labeled = ({ label, ...props }: TxtProps & { label: string }) => <Field label={label}><Txt {...props} /></Field>

function Pick<T extends string>({ value, onChange, choices, aria, className, disabled = [] }: { value: T; onChange: (v: T) => void; choices: Record<T, string>; aria: string; className?: string; disabled?: T[] }) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as T)}>
      <SelectTrigger className={className ?? "w-full"} aria-label={aria}><SelectValue /></SelectTrigger>
      <SelectContent>{(Object.keys(choices) as T[]).map((k) => <SelectItem key={k} value={k} disabled={disabled.includes(k)}>{choices[k]}</SelectItem>)}</SelectContent>
    </Select>
  )
}

/** Move up, move down and remove for row `i` of `n`. */
function Tools({ what, i, n, onMove, onRemove, canRemove = true }: { what: string; i: number; n: number; onMove: (by: number) => void; onRemove: () => void; canRemove?: boolean }) {
  return (
    <div className="flex shrink-0 items-center">
      <Button type="button" size="icon-xs" variant="ghost" aria-label={`Move ${what} up`} disabled={i === 0} onClick={() => onMove(-1)}><ArrowUp /></Button>
      <Button type="button" size="icon-xs" variant="ghost" aria-label={`Move ${what} down`} disabled={i === n - 1} onClick={() => onMove(1)}><ArrowDown /></Button>
      <Button type="button" size="icon-xs" variant="ghost" aria-label={`Remove ${what}`} disabled={!canRemove} onClick={onRemove}><X /></Button>
    </div>
  )
}

/** Field rows: label, kind, max characters, tools. On a phone the label and tools share a line, kind and max the next. */
const ROW = "grid items-start gap-2 max-sm:grid-cols-[1fr_auto] sm:grid-cols-[1fr_8rem_6rem_4.5rem]"
/** An input with a caption of its own on a phone; on wider screens the header row above names it. */
const Cell = ({ caption, children }: { caption: string; children: React.ReactNode }) => (
  <div className="min-w-0 space-y-1.5"><span className="block h-4 text-xs text-muted-foreground sm:hidden">{caption}</span>{children}</div>
)

const swap = <T,>(list: T[], i: number, f: (x: T) => T) => list.map((x, j) => (j === i ? f(x) : x))
const drop = <T,>(list: T[], i: number) => list.filter((_, j) => j !== i)

/** The menu as rows: its name, then each option with its price and fields. `errors` name the input they belong to. */
export function MenuEditor({ draft, errors, onChange }: { draft: MenuDraft; errors: MenuErrors; onChange: (d: MenuDraft) => void }) {
  const e = errors.at
  const [open, setOpen] = useState<string | null>(draft.options.length === 1 ? draft.options[0].id : null)
  // A refusal opens the option it names.
  const bad = draft.options.find((_, i) => Object.keys(e).some((k) => k === `options.${i}` || k.startsWith(`options.${i}.`)))?.id
  const [seen, setSeen] = useState(bad)
  if (bad !== seen) { setSeen(bad); if (bad) setOpen(bad) }
  const setOption = (i: number, f: (o: OptionDraft) => OptionDraft) => onChange({ ...draft, options: swap(draft.options, i, f) })
  return (
    <div className="space-y-4">
      <Labeled label="Name" aria="Menu name" maxLength={LIMITS.name} value={draft.name} error={e.name} onChange={(name) => onChange({ ...draft, name })} placeholder="Support" />
      <div className="space-y-1">
        <div className="flex h-8 items-center justify-between gap-3">
          <span className="text-sm font-medium">Options</span>
          <Button type="button" size="xs" variant="outline" disabled={draft.options.length >= LIMITS.options} onClick={() => { const o = newOption(); onChange({ ...draft, options: [...draft.options, o] }); setOpen(o.id) }}>Add option</Button>
        </div>
        <Err>{e.options}</Err>
        <div className="divide-y">
          {draft.options.map((o, i) => (
            <OptionRow
              key={o.id} o={o} i={i} n={draft.options.length} at={e} open={open === o.id} onToggle={() => setOpen(open === o.id ? null : o.id)}
              onChange={(f) => setOption(i, f)}
              onMove={(by) => onChange({ ...draft, options: move(draft.options, i, by) })}
              onRemove={() => onChange({ ...draft, options: drop(draft.options, i) })}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

type At = Record<string, string>

const credit = (v: string) => `${v || "?"} ${v === "1" ? "credit" : "credits"}`
/** An option's price in a few words, for its collapsed row. */
function priceText(p: OptionDraft["price"]): string {
  if (p.kind === "fixed") return credit(p.amount.trim())
  if (p.kind === "range") return `${p.min.trim() || "?"}-${p.max.trim() || "?"} credits`
  if (p.kind === "quoted") return "Quoted later"
  return `${p.steps.length} prices`
}

/** One option: a row that opens to its label, price and fields; only one is open at a time. */
function OptionRow({ o, i, n, at, open, onToggle, onChange, onMove, onRemove }: {
  o: OptionDraft; i: number; n: number; at: At; open: boolean; onToggle: () => void
  onChange: (f: (o: OptionDraft) => OptionDraft) => void; onMove: (by: number) => void; onRemove: () => void
}) {
  const p = `options.${i}`
  const name = `option ${i + 1}`
  const failed = Object.keys(at).some((k) => k === p || k.startsWith(`${p}.`))
  return (
    <div className="py-1">
      <div className="flex min-h-11 items-center gap-2">
        <button
          type="button" aria-expanded={open} aria-label={`${open ? "Close" : "Open"} ${name}`} onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-md py-1.5 text-left text-sm hover:text-foreground"
        >
          <ChevronDown className={cn("size-4 shrink-0 text-muted-foreground transition-transform", !open && "-rotate-90")} />
          <span className={cn("min-w-0 truncate font-medium", !o.label && "text-muted-foreground")}>{o.label || `Option ${i + 1}`}</span>
          <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular">{priceText(o.price)}</span>
          {failed && !open && <span className="size-1.5 shrink-0 rounded-full bg-destructive" role="img" aria-label="Has an error" />}
        </button>
        <Tools what={name} i={i} n={n} onMove={onMove} onRemove={onRemove} canRemove={n > 1} />
      </div>
      {open && (
        <div className="space-y-3 pb-3 pl-6">
          <Labeled label="Label" aria={`Label of ${name}`} maxLength={LIMITS.label} value={o.label} error={at[`${p}.label`]} placeholder="Review this thread" onChange={(v) => onChange((x) => ({ ...x, label: v }))} />
          <PriceEditor o={o} name={name} p={p} at={at} onChange={onChange} />
          <FieldsEditor o={o} name={name} p={p} at={at} onChange={onChange} />
          <Err>{at[p]}</Err>
        </div>
      )}
    </div>
  )
}

function PriceEditor({ o, name, p, at, onChange }: { o: OptionDraft; name: string; p: string; at: At; onChange: (f: (o: OptionDraft) => OptionDraft) => void }) {
  const price = o.price
  const set = (patch: Partial<OptionDraft["price"]>) => onChange((x) => ({ ...x, price: { ...x.price, ...patch } }))
  const money = (label: string, key: "amount" | "min" | "max", placeholder?: string) => (
    <Labeled label={label} aria={`${label} of ${name}`} inputMode="decimal" value={price[key]} error={at[`${p}.price.${key}`]} placeholder={placeholder} onChange={(v) => set({ [key]: v })} />
  )
  const setStep = (j: number, f: (s: StepDraft) => StepDraft) => set({ steps: swap(price.steps, j, f) })
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-3 max-sm:grid-cols-2">
        <div className={price.kind === "range" || price.kind === "quoted" ? "max-sm:col-span-2" : undefined}><Field label="Price"><Pick value={price.kind} onChange={(kind: PriceKind) => set({ kind })} choices={PRICES} aria={`Price of ${name}`} disabled={o.fields.length >= LIMITS.fields && price.kind !== "steps" ? ["steps"] : []} /></Field></div>
        {price.kind === "fixed" && money("Credits", "amount", "1")}
        {price.kind === "range" && <>{money("Min", "min", "1")}{money("Max", "max", "3")}</>}
        {price.kind === "quoted" && <>{money("Min", "min", "0")}{money("Max", "max", "No limit")}</>}
        {price.kind === "steps" && <div className="col-span-2"><Labeled label="What to pick" aria={`What to pick in ${name}`} maxLength={LIMITS.fieldLabel} value={price.label} error={at[`${p}.price.label`]} placeholder="Length" onChange={(label) => set({ label })} /></div>}
      </div>
      {price.kind === "steps" && (
        <div className="space-y-2">
          {price.steps.map((s, j) => (
            <div key={s.id} className="flex items-start gap-2">
              <div className="grid min-w-0 flex-1 grid-cols-[1fr_6rem] gap-2">
                <Txt aria={`Step ${j + 1} of ${name}`} maxLength={LIMITS.label} placeholder="30 minutes" value={s.label} error={at[`${p}.price.steps.${j}.label`]} onChange={(label) => setStep(j, (x) => ({ ...x, label }))} />
                <Txt aria={`Step ${j + 1} credits of ${name}`} inputMode="decimal" placeholder="1" value={s.amount} error={at[`${p}.price.steps.${j}.amount`]} onChange={(amount) => setStep(j, (x) => ({ ...x, amount }))} />
              </div>
              <Tools what={`step ${j + 1} of ${name}`} i={j} n={price.steps.length} onMove={(by) => set({ steps: move(price.steps, j, by) })} onRemove={() => set({ steps: drop(price.steps, j) })} canRemove={price.steps.length > 2} />
            </div>
          ))}
          <Err>{at[`${p}.price.steps`]}</Err>
          <Button type="button" size="xs" variant="outline" disabled={price.steps.length >= LIMITS.steps || used(o) > LIMITS.fields} onClick={() => set({ steps: [...price.steps, newStep()] })}>Add step</Button>
        </div>
      )}
      <Err>{at[`${p}.price`]}</Err>
    </div>
  )
}

function FieldsEditor({ o, name, p, at, onChange }: { o: OptionDraft; name: string; p: string; at: At; onChange: (f: (o: OptionDraft) => OptionDraft) => void }) {
  const set = (fields: FieldDraft[]) => onChange((x) => ({ ...x, fields }))
  const setField = (k: number, f: (x: FieldDraft) => FieldDraft) => set(swap(o.fields, k, f))
  return (
    <div className="space-y-2">
      <div className="flex h-6 items-center justify-between gap-3">
        <span className="text-xs font-medium text-muted-foreground">Fields</span>
        <Button type="button" size="xs" variant="outline" disabled={used(o) >= LIMITS.fields} aria-label={`Add field to ${name}`} onClick={() => set([...o.fields, newField()])}>Add field</Button>
      </div>
      <Err>{at[`${p}.fields`]}</Err>
      {o.fields.length > 0 && (
        <div className={`${ROW} text-xs text-muted-foreground max-sm:hidden`}>
          <span>Label</span><span>Kind</span><span>Max characters</span>
        </div>
      )}
      {o.fields.map((f, k) => {
        const q = `${p}.fields.${k}`
        const what = `field ${k + 1} of ${name}`
        return (
          <div key={f.id} className="space-y-2">
            <div className={ROW}>
              <Cell caption="Label"><Txt aria={`Label of ${what}`} maxLength={LIMITS.fieldLabel} placeholder="What do you need?" value={f.label} error={at[`${q}.label`]} onChange={(label) => setField(k, (x) => ({ ...x, label }))} /></Cell>
              <div className="grid grid-cols-2 gap-2 max-sm:order-3 max-sm:col-span-2 sm:contents">
                <Cell caption="Kind"><Pick value={f.kind} onChange={(kind: FieldKind) => setField(k, (x) => ({ ...x, kind }))} choices={FIELDS} aria={`Kind of ${what}`} /></Cell>
                {f.kind !== "choice" && (
                  <Cell caption="Max characters">
                    <Txt aria={`Max characters of ${what}`} inputMode="numeric" placeholder={f.kind === "text" ? `Max ${LIMITS.text}` : `Max ${LIMITS.paragraph}`} value={f.max} error={at[`${q}.max_chars`]} onChange={(max) => setField(k, (x) => ({ ...x, max: max.replace(/\D/g, "") }))} />
                  </Cell>
                )}
              </div>
              <div className="max-sm:order-2 max-sm:mt-5 sm:justify-self-end">
                <Tools what={what} i={k} n={o.fields.length} onMove={(by) => set(move(o.fields, k, by))} onRemove={() => set(drop(o.fields, k))} />
              </div>
            </div>
            {f.kind === "choice" && (
              <div className="space-y-1">
                <Textarea aria-label={`Choices of ${what}`} aria-invalid={!!at[`${q}.choices`]} className="min-h-16" placeholder="One choice per line" value={f.choices} onChange={(e) => setField(k, (x) => ({ ...x, choices: e.target.value }))} />
                <Err>{at[`${q}.choices`]}</Err>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

import { ArrowDown, ArrowUp, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { Field } from "@/modules/admin/shared"
import {
  cleanKey, FIELDS, LIMITS, move, newField, newOption, newStep, PRICES, used,
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

function Pick<T extends string>({ value, onChange, choices, aria, className }: { value: T; onChange: (v: T) => void; choices: Record<T, string>; aria: string; className?: string }) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v as T)}>
      <SelectTrigger className={className ?? "w-full"} aria-label={aria}><SelectValue /></SelectTrigger>
      <SelectContent>{(Object.keys(choices) as T[]).map((k) => <SelectItem key={k} value={k}>{choices[k]}</SelectItem>)}</SelectContent>
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

const swap = <T,>(list: T[], i: number, f: (x: T) => T) => list.map((x, j) => (j === i ? f(x) : x))
const drop = <T,>(list: T[], i: number) => list.filter((_, j) => j !== i)

/** The menu as rows: its name, then each option with its price and fields. `errors` name the input they belong to. */
export function MenuEditor({ draft, errors, onChange }: { draft: MenuDraft; errors: MenuErrors; onChange: (d: MenuDraft) => void }) {
  const e = errors.at
  const setOption = (i: number, f: (o: OptionDraft) => OptionDraft) => onChange({ ...draft, options: swap(draft.options, i, f) })
  return (
    <div className="space-y-4">
      <Labeled label="Name" aria="Menu name" maxLength={LIMITS.name} value={draft.name} error={e.name} onChange={(name) => onChange({ ...draft, name })} placeholder="Support" />
      <div className="space-y-1">
        <div className="flex h-8 items-center justify-between gap-3">
          <span className="text-sm font-medium">Options</span>
          <Button type="button" size="xs" variant="outline" disabled={draft.options.length >= LIMITS.options} onClick={() => onChange({ ...draft, options: [...draft.options, newOption()] })}>Add option</Button>
        </div>
        <Err>{e.options}</Err>
        <div className="divide-y">
          {draft.options.map((o, i) => (
            <OptionRow
              key={o.id} o={o} i={i} n={draft.options.length} at={e}
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

function OptionRow({ o, i, n, at, onChange, onMove, onRemove }: {
  o: OptionDraft; i: number; n: number; at: At
  onChange: (f: (o: OptionDraft) => OptionDraft) => void; onMove: (by: number) => void; onRemove: () => void
}) {
  const p = `options.${i}`
  const name = `option ${i + 1}`
  return (
    <div className="space-y-3 py-4">
      <div className="flex h-6 items-center justify-between gap-3">
        <span className="text-xs font-medium text-muted-foreground">Option {i + 1}</span>
        <Tools what={name} i={i} n={n} onMove={onMove} onRemove={onRemove} canRemove={n > 1} />
      </div>
      <div className="grid grid-cols-[10rem_1fr] gap-3 max-sm:grid-cols-1">
        <Labeled label="Key" aria={`Key of ${name}`} value={o.key} error={at[`${p}.key`]} placeholder="review" onChange={(v) => onChange((x) => ({ ...x, key: cleanKey(v) }))} />
        <Labeled label="Label" aria={`Label of ${name}`} maxLength={LIMITS.label} value={o.label} error={at[`${p}.label`]} placeholder="Review this thread" onChange={(v) => onChange((x) => ({ ...x, label: v }))} />
      </div>
      <PriceEditor o={o} name={name} p={p} at={at} onChange={onChange} />
      <FieldsEditor o={o} name={name} p={p} at={at} onChange={onChange} />
      <Err>{at[p]}</Err>
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
        <Field label="Price"><Pick value={price.kind} onChange={(kind: PriceKind) => set({ kind })} choices={PRICES} aria={`Price of ${name}`} /></Field>
        {price.kind === "fixed" && money("Credits", "amount", "1")}
        {price.kind === "range" && <>{money("Min", "min", "1")}{money("Max", "max", "3")}</>}
        {price.kind === "quoted" && <>{money("Min", "min", "0")}{money("Max", "max", "No limit")}</>}
        {price.kind === "steps" && <div className="col-span-2"><Labeled label="Question" aria={`Question of ${name}`} maxLength={LIMITS.fieldLabel} value={price.label} error={at[`${p}.price.label`]} placeholder="Length" onChange={(label) => set({ label })} /></div>}
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
      {o.fields.map((f, k) => {
        const q = `${p}.fields.${k}`
        const what = `field ${k + 1} of ${name}`
        return (
          <div key={f.id} className="space-y-2">
            <div className="flex items-start gap-2">
              <div className="flex min-w-0 flex-1 flex-wrap gap-2">
                <div className="min-w-40 flex-1"><Txt aria={`Label of ${what}`} maxLength={LIMITS.fieldLabel} placeholder="What do you need?" value={f.label} error={at[`${q}.label`]} onChange={(label) => setField(k, (x) => ({ ...x, label }))} /></div>
                <Pick value={f.kind} onChange={(kind: FieldKind) => setField(k, (x) => ({ ...x, kind }))} choices={FIELDS} aria={`Kind of ${what}`} className="w-32" />
                {f.kind !== "choice" && (
                  <div className="w-24">
                    <Txt aria={`Max characters of ${what}`} inputMode="numeric" placeholder={f.kind === "text" ? `Max ${LIMITS.text}` : `Max ${LIMITS.paragraph}`} value={f.max} error={at[`${q}.max_chars`]} onChange={(max) => setField(k, (x) => ({ ...x, max: max.replace(/\D/g, "") }))} />
                  </div>
                )}
              </div>
              <Tools what={what} i={k} n={o.fields.length} onMove={(by) => set(move(o.fields, k, by))} onRemove={() => set(drop(o.fields, k))} />
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

// A menu for Ask a person as the editor holds it: strings and stable row ids, turned into and out of the API's catalog.
// Limits and key rules follow `daimon.core.support_plans`.

export const LIMITS = { options: 10, fields: 4, steps: 10, name: 75, label: 75, fieldLabel: 45, text: 200, paragraph: 3000 }
export const PRICES = { fixed: "Fixed", steps: "Steps", range: "Range", quoted: "Quoted" } as const
export const FIELDS = { text: "Text", paragraph: "Paragraph", choice: "Choice" } as const
export type PriceKind = keyof typeof PRICES
export type FieldKind = keyof typeof FIELDS

type Bag = Record<string, unknown>
export type StepDraft = { id: string; label: string; amount: string }
export type FieldDraft = { id: string; key: string; label: string; kind: FieldKind; max: string; choices: string; rest: Bag }
export type OptionDraft = {
  id: string; key: string; label: string; rest: Bag
  price: { kind: PriceKind; amount: string; min: string; max: string; label: string; steps: StepDraft[] }
  fields: FieldDraft[]
}
export type MenuDraft = { name: string; options: OptionDraft[] }

let counter = 0
const uid = () => `r${++counter}`
const bag = (v: unknown): Bag => (v && typeof v === "object" && !Array.isArray(v) ? (v as Bag) : {})
const str = (v: unknown) => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "")

/** A key as typed: lowercase, spaces to `sep`, nothing else. */
export const cleanKey = (v: string, sep: "-" | "_" = "_") => v.toLowerCase().replace(/[\s_-]+/g, sep).replace(sep === "_" ? /[^a-z0-9_]/g : /[^a-z0-9-]/g, "").slice(0, 32)
const slug = (v: string, sep: "-" | "_" = "_") => cleanKey(v, sep).replace(/^[-_]+|[-_]+$/g, "")

export const newStep = (): StepDraft => ({ id: uid(), label: "", amount: "" })
export const newField = (): FieldDraft => ({ id: uid(), key: "", label: "", kind: "text", max: "", choices: "", rest: {} })
export const newOption = (): OptionDraft => ({
  id: uid(), key: "", label: "", rest: {}, fields: [],
  price: { kind: "fixed", amount: "1", min: "", max: "", label: "", steps: [newStep(), newStep()] },
})
export const emptyMenu = (): MenuDraft => ({ name: "", options: [newOption()] })

/** Fields an option holds in all: a steps price counts as one. */
export const used = (o: OptionDraft) => o.fields.length + (o.price.kind === "steps" ? 1 : 0)

export function toDraft(catalog: unknown): MenuDraft {
  const c = bag(catalog)
  const options = (Array.isArray(c.options) ? c.options : []).map((raw): OptionDraft => {
    const { key, label, price: p, fields, ...rest } = bag(raw)
    const price = bag(p)
    const kind = (price.kind as PriceKind) in PRICES ? (price.kind as PriceKind) : "fixed"
    return {
      id: uid(), key: str(key), label: str(label), rest,
      price: {
        kind, amount: str(price.amount), min: str(price.min), max: str(price.max), label: str(price.label),
        steps: (Array.isArray(price.steps) ? price.steps : []).map((s) => ({ id: uid(), label: str(bag(s).label), amount: str(bag(s).amount) })),
      },
      fields: (Array.isArray(fields) ? fields : []).map((f): FieldDraft => {
        const { key: k, label: l, kind: fk, max_chars: max, choices, ...frest } = bag(f)
        return {
          id: uid(), key: str(k), label: str(l), kind: (fk as FieldKind) in FIELDS ? (fk as FieldKind) : "text",
          max: str(max), choices: (Array.isArray(choices) ? choices : []).map(str).join("\n"), rest: frest,
        }
      }),
    }
  })
  return { name: str(c.name), options: options.length ? options : [newOption()] }
}

const num = (v: string) => (/^\d+$/.test(v.trim()) ? Number(v) : v.trim())

function priceOf(o: OptionDraft): Bag {
  const p = o.price
  switch (p.kind) {
    case "fixed": return { kind: "fixed", amount: p.amount.trim() }
    case "steps": return { kind: "steps", label: p.label.trim(), steps: p.steps.map((s) => ({ label: s.label.trim(), amount: s.amount.trim() })) }
    case "range": return { kind: "range", min: p.min.trim(), max: p.max.trim() }
    case "quoted": return { kind: "quoted", ...(p.min.trim() && { min: p.min.trim() }), ...(p.max.trim() && { max: p.max.trim() }) }
  }
}

function fieldsOf(o: OptionDraft): Bag[] {
  const taken = new Set(o.fields.map((f) => f.key).filter(Boolean))
  return o.fields.map((f) => {
    let key = f.key
    if (!key) {
      const base = slug(f.label) || "field"
      key = base
      for (let n = 2; taken.has(key); n++) key = `${base.slice(0, 29)}_${n}`
      taken.add(key)
    }
    const out: Bag = { ...f.rest, key, label: f.label.trim(), kind: f.kind }
    if (f.kind === "choice") out.choices = f.choices.split("\n").map((c) => c.trim()).filter(Boolean)
    else if (f.max.trim()) out.max_chars = num(f.max)
    return out
  })
}

/** The API's catalog; a blank key is made from the label. */
export function toCatalog(m: MenuDraft): Bag {
  return {
    name: m.name.trim(),
    options: m.options.map((o) => ({
      ...o.rest, key: o.key || slug(o.label), label: o.label.trim(), price: priceOf(o),
      ...(o.fields.length && { fields: fieldsOf(o) }),
    })),
  }
}

const TAGS = new Set<string>([...Object.keys(PRICES), ...Object.keys(FIELDS)])

/** Input paths a draft has, e.g. `options.0.price.amount`. */
function inputs(m: MenuDraft): Set<string> {
  const paths = new Set(["name", "options"])
  m.options.forEach((o, i) => {
    const at = `options.${i}`
    for (const p of ["", ".key", ".label", ".price", ".price.amount", ".price.min", ".price.max", ".price.label", ".price.steps", ".fields"]) paths.add(at + p)
    o.price.steps.forEach((_, j) => ["label", "amount"].forEach((p) => paths.add(`${at}.price.steps.${j}.${p}`)))
    o.fields.forEach((_, k) => ["label", "max_chars", "choices"].forEach((p) => paths.add(`${at}.fields.${k}.${p}`)))
  })
  return paths
}

/** A location from the server as an input path: the kind tags are dropped and a choice's index folds into `choices`. */
function pathOf(loc: string): string {
  const t = loc.split(".")
  if (t[2] === "price" && TAGS.has(t[3])) t.splice(3, 1)
  else if (t[2] === "fields" && TAGS.has(t[4])) t.splice(4, 1)
  const at = t.indexOf("choices")
  if (t[2] === "fields" && at > 0) t.length = at + 1
  if (t[2] === "fields" && t[4] === "key") t[4] = "label"
  return t.join(".")
}

export type MenuErrors = { at: Record<string, string>; rest: string | null }

/** Splits "Catalog not valid: options.0.label: ...; ..." into the message for each input, and what names none. */
export function menuErrors(message: string | null, m: MenuDraft): MenuErrors {
  const at: Record<string, string> = {}
  if (!message) return { at, rest: null }
  const head = "Catalog not valid: "
  if (!message.startsWith(head)) return { at, rest: message }
  const known = inputs(m)
  const rest: string[] = []
  for (const part of message.slice(head.length).split("; ")) {
    const hit = /^([a-z0-9_.]+): (.+)$/.exec(part)
    let path = hit ? pathOf(hit[1]) : ""
    const text = hit ? hit[2] : part
    if (!known.has(path)) path = /^options\.\d+/.exec(path)?.[0] ?? ""
    if (!path) rest.push(part)
    else at[path] = at[path] ? `${at[path]}. ${text}` : text
  }
  return { at, rest: rest.length ? `${head}${rest.join("; ")}` : null }
}

/** Moves a row `by` places; out of range leaves the list alone. */
export function move<T>(list: T[], from: number, by: number): T[] {
  const to = from + by
  if (to < 0 || to >= list.length) return list
  const next = [...list]
  next.splice(to, 0, ...next.splice(from, 1))
  return next
}

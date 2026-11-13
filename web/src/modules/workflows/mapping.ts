// A webhook trigger's `inputs` expression, evaluated against a delivery, and the result checked against the workflow's inputs.
import type { ObjectSchema } from "./types"

export type Delivered = { headers: Record<string, string>; payload: unknown }
export type Mapped = { ok: true; value: unknown } | { ok: false; error: string }

/** `${{ payload.data.items[0].id }}` or `${{ headers.x-id }}`: a path into the delivery. */
export function evalMapping(expr: string, d: Delivered): Mapped {
  const m = expr.trim().match(/^\$\{\{\s*(.+?)\s*\}\}$/)
  if (!m) return { ok: false, error: "Use ${{ payload.path }}" }
  const parts = m[1]!.match(/[^.[\]]+/g) ?? []
  const root = parts.shift()
  if (root !== "payload" && root !== "headers") return { ok: false, error: `Unknown name ${root}` }
  let v: unknown = root === "payload" ? d.payload : d.headers
  const seen: string[] = [root]
  for (const p of parts) {
    seen.push(p)
    if (v == null || typeof v !== "object" || !(p in v)) return { ok: false, error: `No ${seen.join(".")} in this delivery` }
    v = (v as Record<string, unknown>)[p]
  }
  return { ok: true, value: v }
}

export type Row = { field: string; value: unknown; state: "ok" | "missing" | "wrong_type" | "empty" }
export type Checked = { rows: Row[]; extra: string[]; valid: boolean }

const typeOf = (v: unknown) => (Array.isArray(v) ? "array" : v === null ? "null" : typeof v === "number" ? (Number.isInteger(v) ? "integer" : "number") : typeof v)
const fits = (t: string | undefined, v: unknown) => !t || typeOf(v) === t || (t === "number" && typeOf(v) === "integer")

/** One row per input the workflow declares, then the keys the mapping produced that it does not. */
export function checkInputs(schema: ObjectSchema | undefined, value: unknown): Checked {
  const obj = value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
  const props = schema?.properties ?? {}
  const rows: Row[] = Object.entries(props).map(([field, f]) => {
    const v = obj[field]
    const state: Row["state"] = v === undefined ? (schema?.required?.includes(field) ? "missing" : "empty") : fits(f.type, v) ? "ok" : "wrong_type"
    return { field, value: v, state }
  })
  const extra = Object.keys(obj).filter((k) => !(k in props))
  return { rows, extra, valid: rows.every((r) => r.state === "ok" || r.state === "empty") }
}

/** An expression that fills `inputs` from a captured payload: each declared input found by name at the top of `payload`, else under `payload.data`. */
export function suggestMapping(schema: ObjectSchema | undefined, payload: unknown): string {
  const keys = Object.keys(schema?.properties ?? {})
  const has = (o: unknown) => !!o && typeof o === "object" && keys.some((k) => k in (o as object))
  if (has(payload)) return "${{ payload }}"
  const data = payload && typeof payload === "object" ? (payload as Record<string, unknown>).data : undefined
  return has(data) ? "${{ payload.data }}" : "${{ payload }}"
}

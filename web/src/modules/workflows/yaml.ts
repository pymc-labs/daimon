import { isMap, parseDocument, stringify } from "yaml"

import type { Spec } from "./types"

export const toYaml = (spec: Spec) => stringify(spec, { lineWidth: 0, flowCollectionPadding: false })

export type Parsed = { spec: Spec; error?: undefined } | { spec?: undefined; error: string; line: number }

/** Parse YAML into a spec; the first error carries its 1-based line. */
export function fromYaml(text: string): Parsed {
  const doc = parseDocument(text, { prettyErrors: false })
  const e = doc.errors[0]
  if (e) return { error: e.message.split("\n")[0]!.replace(/ at line \d+, column \d+:?$/, ""), line: lineAt(text, e.pos[0]) }
  if (!isMap(doc.contents)) return { error: "Expected a mapping", line: 1 }
  const spec = doc.toJS() as Spec
  for (const k of ["name", "start", "nodes"] as const) if (!spec[k]) return { error: `Missing ${k}`, line: 1 }
  if (typeof spec.nodes !== "object") return { error: "nodes must be a mapping", line: 1 }
  return { spec }
}

const lineAt = (text: string, pos: number) => text.slice(0, pos).split("\n").length

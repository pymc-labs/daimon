import { describe, expect, it } from "vitest"

import { SPECS } from "@/mock/workflows-specs"
import fixtures from "./fixtures/validation.json"
import { validate } from "./spec"
import type { Spec } from "./types"

// The server validator (daimon.core.workflows.validate) reads the same cases; the canvas
// checks the subset of rules in `client_codes`.
type Case = {
  name: string; spec: unknown; seed?: string; server_only?: boolean
  errors: string[]; warnings: string[]; grants?: string[]; classes?: Record<string, string[]>
}
const { client_codes, cases } = fixtures as unknown as { client_codes: string[]; cases: Case[] }
const client = new Set(client_codes)
const keep = (xs: string[]) => [...new Set(xs.filter((x) => client.has(x.split(":")[0]!)))].sort()
const keys = (ps: { code: string; node?: string }[]) => keep(ps.map((p) => `${p.code}:${p.node ?? ""}`))

describe("shared validation fixtures", () => {
  for (const c of cases.filter((x) => !x.server_only)) {
    it(c.name, () => {
      const spec = c.spec as Spec
      if (c.seed) expect(spec, "fixture seed matches the mock seed").toEqual(JSON.parse(JSON.stringify(SPECS[c.seed as keyof typeof SPECS])))
      const v = validate(spec)
      expect(keys(v.errors), "errors").toEqual(keep(c.errors))
      expect(keys(v.warnings), "warnings").toEqual(keep(c.warnings))
      if (c.grants) expect([...new Set(v.grants.map((g) => `${g.kind}:${g.node ?? ""}`))].sort(), "grants").toEqual(c.grants)
      if (c.classes) expect(v.classes, "classes").toEqual(c.classes)
    })
  }
})

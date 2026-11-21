// Publish requests, kept in the tab's session so a request made as one person is still there when another signs in.
import type { Grant, Person, PublishRequest, Spec } from "@/modules/workflows/types"

export type Stored = PublishRequest & { spec: Spec }
const KEY = "daimon:mock:publish-requests"

const load = (): Stored[] => {
  try { return JSON.parse(sessionStorage.getItem(KEY) ?? "[]") as Stored[] } catch { return [] }
}
const save = (rows: Stored[]) => sessionStorage.setItem(KEY, JSON.stringify(rows))
const strip = ({ spec: _spec, ...r }: Stored): PublishRequest => r

export const requests = {
  all: load,
  find: (id: string) => load().find((r) => r.id === id),
  add(r: { workflow: PublishRequest["workflow"]; version: number; note: string; by: Person; revision: number; grants: Grant[]; spec: Spec }): Stored {
    const rows = load().filter((x) => !(x.workflow.id === r.workflow.id && x.status === "pending"))
    const row: Stored = { ...r, id: `req_${Date.now().toString(36)}`, at: new Date().toISOString(), status: "pending", decided_by: null, decided_at: null, comment: null }
    save([row, ...rows])
    return row
  },
  decide(id: string, status: "approved" | "rejected", by: Person, comment: string | null) {
    const rows = load()
    const r = rows.find((x) => x.id === id)
    if (!r) return undefined
    Object.assign(r, { status, decided_by: by, decided_at: new Date().toISOString(), comment })
    save(rows)
    return r
  },
}
export const pendingOf = (wf: string): PublishRequest | null => {
  const r = load().find((x) => x.workflow.id === wf && x.status === "pending")
  return r ? strip(r) : null
}
export const publicRequest = strip

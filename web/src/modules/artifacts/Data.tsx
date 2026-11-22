import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ChevronLeft, Download } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Ago } from "@/components/common"
import { Empty } from "@/components/filters"
import { PanelSection } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { useParam, rowProps } from "@/lib/filters"
import { exact } from "@/lib/format"
import { cn } from "@/lib/utils"
import { bytes } from "../chat/format"
import { KEY, useChange } from "./bits"
import type { ArtifactDetail, CollectionRule, DataCollection, DataDocument } from "./types"

const Meter = ({ label, value, of }: { label: string; value: string; of: string }) => (
  <div className="flex-1">
    <div className="text-xs text-muted-foreground">{label}</div>
    <div className="mt-0.5 text-sm tabular"><span className="font-medium">{value}</span><span className="text-muted-foreground"> of {of}</span></div>
  </div>
)

const ANYONE: CollectionRule = { read: "all", write: "all" }
const CHOICES = {
  read: [["all", "All"], ["own", "Their own"]],
  write: [["all", "All"], ["own", "Their own"], ["none", "None"]],
} as const

/** What the page's viewers may read and change here; managers and the agent are not bound. */
function Rules({ a, collection }: { a: ArtifactDetail; collection: string }) {
  const rule = a.collection_rules[collection] ?? ANYONE
  const save = useChange(a.id, () => toast("Rules saved"))
  const set = (next: CollectionRule) => {
    const { [collection]: _, ...rest } = a.collection_rules
    save.mutate({ collection_rules: next.read === "all" && next.write === "all" ? rest : { ...rest, [collection]: next } })
  }
  return (
    <PanelSection title="Viewers can" className="space-y-2.5 py-3">
      {(["read", "write"] as const).map((k) => (
        <div key={k} role="radiogroup" aria-label={k === "read" ? "Viewers read" : "Viewers write"} className="flex items-center gap-2">
          <span className="w-10 shrink-0 text-xs text-muted-foreground">{k === "read" ? "Read" : "Write"}</span>
          <div className={cn("grid flex-1 gap-1.5", k === "read" ? "grid-cols-2" : "grid-cols-3")}>
            {CHOICES[k].map(([v, label]) => {
              const on = rule[k] === v
              return (
                <button
                  key={v} type="button" role="radio" aria-checked={on} disabled={save.isPending}
                  onClick={() => !on && set({ ...rule, [k]: v })}
                  className={cn("h-9 rounded-lg border px-2 text-xs font-medium transition-colors", on ? "border-primary bg-accent/50 ring-1 ring-primary" : "hover:bg-accent/50")}
                >{label}</button>
              )
            })}
          </div>
        </div>
      ))}
    </PanelSection>
  )
}

function download(name: string, rows: DataDocument[]) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(rows, null, 2)], { type: "application/json" }))
  Object.assign(document.createElement("a"), { href: url, download: `${name}.json` }).click()
  URL.revokeObjectURL(url)
}

function Doc({ a, collection, doc, back }: { a: ArtifactDetail; collection: string; doc: DataDocument; back: () => void }) {
  const qc = useQueryClient()
  const confirm = useConfirm()
  const [text, setText] = useState<string | null>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: [...KEY, "data", a.id] })
  const path = `/api/artifacts/${a.id}/data/${collection}/${doc.id}`
  const save = useMutation({
    mutationFn: (data: unknown) => api.put(path, { data }),
    onSuccess: () => { void refresh(); setText(null); toast("Document saved") },
  })
  const del = useMutation({ mutationFn: () => api.del(path), onSuccess: () => { void refresh(); back(); toast("Document deleted") } })
  let bad = false
  if (text !== null) try { JSON.parse(text) } catch { bad = true }
  return (
    <PanelSection className="space-y-3 border-t-0 pt-0">
      <button onClick={back} className="-ml-1 flex h-7 items-center gap-0.5 text-xs text-muted-foreground hover:text-foreground"><ChevronLeft className="size-3.5" />{collection}</button>
      <div className="flex items-baseline justify-between gap-2"><span className="truncate font-mono text-[12.5px] font-medium">{doc.id}</span><span className="shrink-0 text-xs text-muted-foreground">{bytes(doc.size_bytes)}</span></div>
      {text === null ? (
        <pre className="max-h-96 overflow-auto rounded-md border bg-muted p-3 font-mono text-xs leading-5 whitespace-pre-wrap break-words">{JSON.stringify(doc.data, null, 2)}</pre>
      ) : (
        <Textarea value={text} onChange={(e) => setText(e.target.value)} aria-invalid={bad} spellCheck={false} className="h-72 font-mono text-xs leading-5" />
      )}
      {a.can.edit && (
        <div className="flex justify-end gap-2">
          {text === null ? (
            <>
              <Button variant="outline" size="sm" className="text-destructive" disabled={del.isPending} onClick={async () => { if (await confirm({ title: "Delete document?", body: `${collection}/${doc.id}`, action: "Delete", destructive: true })) del.mutate() }}>Delete</Button>
              <Button size="sm" onClick={() => setText(JSON.stringify(doc.data, null, 2))}>Edit</Button>
            </>
          ) : (
            <>
              <Button variant="outline" size="sm" onClick={() => setText(null)}>Cancel</Button>
              <Button size="sm" disabled={bad || save.isPending} onClick={() => save.mutate(JSON.parse(text))}>Save</Button>
            </>
          )}
        </div>
      )}
    </PanelSection>
  )
}

function Documents({ a, collection }: { a: ArtifactDetail; collection: string }) {
  const [id, setId] = useParam("doc")
  const q = useQuery({ queryKey: [...KEY, "data", a.id, collection], queryFn: () => api.get<DataDocument[]>(`/api/artifacts/${a.id}/data/${collection}`) })
  const open = q.data?.find((d) => d.id === id)
  if (open) return <Doc a={a} collection={collection} doc={open} back={() => setId(null)} />
  return (
    <QueryState query={q} empty="No documents" className="py-10">
      {(rows) => (
        <>
          <Table>
            <TableHeader><TableRow className="hover:bg-transparent"><TableHead className="pl-5">Document</TableHead><TableHead>Updated</TableHead><TableHead className="pr-5">By</TableHead></TableRow></TableHeader>
            <TableBody>
              {rows.map((d) => (
                <TableRow key={d.id} {...rowProps(() => setId(d.id))}>
                  <TableCell className="max-w-28 truncate pl-5 font-mono text-xs">{d.id}</TableCell>
                  <TableCell><Ago iso={d.updated_at} /></TableCell>
                  <TableCell className="max-w-24 truncate pr-5 text-muted-foreground">{d.updated_by}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <div className="flex justify-end px-5 py-3">
            <Button variant="outline" size="sm" onClick={() => download(collection, rows)}><Download />Download JSON</Button>
          </div>
        </>
      )}
    </QueryState>
  )
}

export function Data({ a }: { a: ArtifactDetail }) {
  const q = useQuery({ queryKey: [...KEY, "data", a.id], queryFn: () => api.get<DataCollection[]>(`/api/artifacts/${a.id}/data`), enabled: a.data !== "off" })
  const [picked, setCollection] = useParam("collection")
  const [, setDoc] = useParam("doc")
  if (a.data === "off") return <Empty>Shared data is off</Empty>
  const { usage: u } = a
  return (
    <>
      <PanelSection className="flex gap-4 border-t-0">
        <Meter label="Documents" value={exact(u.documents)} of={exact(u.max_documents)} />
        <Meter label="Size" value={bytes(u.bytes)} of={bytes(u.max_bytes)} />
      </PanelSection>
      <QueryState query={q} empty="No documents yet" className="py-10">
        {(cols) => {
          const name = cols.find((c) => c.name === picked)?.name ?? cols[0]!.name
          return (
            <>
              <PanelSection className="py-3">
                <Select value={name} onValueChange={(v) => { setCollection(v === cols[0]!.name ? null : v); setDoc(null) }}>
                  <SelectTrigger size="sm" className="w-full" aria-label="Collection"><SelectValue /></SelectTrigger>
                  <SelectContent>{cols.map((c) => <SelectItem key={c.name} value={c.name}>{c.name}<span className="ml-2 text-faint tabular">{c.documents}</span></SelectItem>)}</SelectContent>
                </Select>
              </PanelSection>
              {a.can.edit && !name.startsWith("_") && <Rules a={a} collection={name} />}
              <Documents key={name} a={a} collection={name} />
            </>
          )
        }}
      </QueryState>
    </>
  )
}

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Plus, Upload } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"

import { Ago, Money } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { rowProps } from "@/lib/filters"
import { E, useQueues, useRuns, useSuites } from "./api"
import { ImportBundle } from "./Bundles"
import { ROLE, RunPill, ScrubPill } from "./bits"
import type { Suite } from "./types"

const slug = (s: string) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")

function NewSuite({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [name, setName] = useState("")
  const go = useNavigate()
  const qc = useQueryClient()
  const make = useMutation({
    mutationFn: () => api.post<Suite>(`${E}/suites`, { id: slug(name), name: name.trim() }),
    onSuccess: (s) => { void qc.invalidateQueries({ queryKey: ["evals", "suites"] }); go(`/evals/suites/${s.id}`) },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>New suite</DialogTitle></DialogHeader>
        <label className="grid gap-1.5 text-sm"><span className="text-muted-foreground">Name</span><Input autoFocus value={name} onChange={(e) => setName(e.target.value)} className="h-8" /></label>
        {make.error && <p role="alert" className="text-sm text-destructive">{make.error.message}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!slug(name) || make.isPending} onClick={() => make.mutate()}>Create</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

const H = ({ children }: { children: React.ReactNode }) => <h2 className="mb-2 text-sm font-medium">{children}</h2>

export function Evals() {
  const go = useNavigate()
  const suites = useSuites()
  const runs = useRuns()
  const queues = useQueues()
  const [adding, setAdding] = useState(false)
  const [importing, setImporting] = useState(false)
  const open = queues.data?.filter((q) => q.queue.status !== "closed" && q.done < q.total) ?? []
  return (
    <Page
      crumbs={[{ label: "Evals" }]}
      actions={<><Button variant="outline" size="sm" onClick={() => setImporting(true)}><Upload />Import</Button><Button size="sm" onClick={() => setAdding(true)}><Plus />New suite</Button></>}
    >
      <div className="mx-auto max-w-[1040px] space-y-8">
        <section>
          <H>Suites</H>
          <QueryState query={suites} empty="No suites">
            {(rows) => (
              <Table>
                <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Name</TableHead><TableHead className="max-sm:hidden">Version</TableHead><TableHead className="max-sm:hidden">Access</TableHead><TableHead>Scrub</TableHead></TableRow></TableHeader>
                <TableBody>
                  {rows.map((s) => (
                    <TableRow key={s.id} {...rowProps(() => go(`/evals/suites/${s.id}`))}>
                      <TableCell className="font-medium">{s.name}</TableCell>
                      <TableCell className="tabular max-sm:hidden">v{s.version}</TableCell>
                      <TableCell className="text-muted-foreground max-sm:hidden">{ROLE[s.role]}</TableCell>
                      <TableCell><ScrubPill status={s.scrub_status} /></TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </QueryState>
        </section>
        {open.length > 0 && (
          <section>
            <H>Review queues</H>
            <Table>
              <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Queue</TableHead><TableHead>Progress</TableHead></TableRow></TableHeader>
              <TableBody>
                {open.map((q) => (
                  <TableRow key={q.ref} {...rowProps(() => go(`/evals/queues/${q.ref}`))}>
                    <TableCell className="font-medium">{q.queue.name}</TableCell>
                    <TableCell className="tabular text-muted-foreground">{q.done} of {q.total}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </section>
        )}
        {!!runs.data?.length && (
          <section>
            <H>Recent runs</H>
            <Table>
              <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Run</TableHead><TableHead>Status</TableHead><TableHead className="max-sm:hidden">Spend</TableHead><TableHead className="max-sm:hidden">Started</TableHead></TableRow></TableHeader>
              <TableBody>
                {runs.data.slice(0, 8).map((r) => (
                  <TableRow key={r.id} {...rowProps(() => go(`/evals/runs/${r.id}`))}>
                    <TableCell><span className="font-medium">{r.name ?? r.id}</span><span className="block text-xs text-muted-foreground">{r.arms.map((a) => a.label).join(", ")}</span></TableCell>
                    <TableCell><RunPill status={r.status} /></TableCell>
                    <TableCell className="max-sm:hidden"><Money usd={r.spend?.total_usd} /></TableCell>
                    <TableCell className="text-muted-foreground max-sm:hidden"><Ago iso={r.started_at} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </section>
        )}
      </div>
      <NewSuite open={adding} onOpenChange={setAdding} />
      <ImportBundle open={importing} onOpenChange={setImporting} />
    </Page>
  )
}

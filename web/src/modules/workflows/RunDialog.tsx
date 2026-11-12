import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useStartRun } from "./api"
import type { Spec } from "./types"

/** Start a run (or a test run of the draft) with the spec's inputs. */
export function RunDialog({ id, spec, test, open, onOpenChange }: { id: string; spec: Spec; test?: boolean; open: boolean; onOpenChange: (o: boolean) => void }) {
  const start = useStartRun(id)
  const navigate = useNavigate()
  const props = Object.entries(spec.inputs?.properties ?? {})
  const [vals, setVals] = useState<Record<string, string>>({})
  const missing = (spec.inputs?.required ?? []).some((k) => !vals[k]?.trim())
  const go = () => start.mutate({ inputs: vals, test }, {
    onSuccess: (r) => { onOpenChange(false); toast(`${test ? "Test run" : "Run"} #${r.number} started`); if (!test) navigate(`/workflows/${id}/runs`) },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="gap-0 p-0 sm:max-w-md">
        <DialogHeader className="border-b px-5 py-4"><DialogTitle>{test ? "Test run" : "Run"} {spec.name}</DialogTitle></DialogHeader>
        <form className="contents" onSubmit={(e) => { e.preventDefault(); if (!missing) go() }}>
          <div className="space-y-4 px-5 py-4">
            {props.map(([k, f]) => (
              <div key={k} className="space-y-1.5">
                <Label htmlFor={`in-${k}`}>{f.title ?? k}{spec.inputs?.required?.includes(k) && <span className="text-faint">Required</span>}</Label>
                <Input id={`in-${k}`} value={vals[k] ?? ""} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} className="h-8" />
              </div>
            ))}
            {!props.length && <p className="text-sm text-muted-foreground">No inputs</p>}
          </div>
          <DialogFooter className="m-0 flex-col items-stretch gap-2 border-t px-5 py-3 sm:flex-col sm:justify-start">
            <div className="flex justify-end gap-2 max-sm:[&>*]:flex-1">
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button type="submit" disabled={missing || start.isPending}>{test ? "Start test run" : "Run"}</Button>
            </div>
            {start.error && <p className="text-right text-sm text-destructive">{start.error.message}</p>}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

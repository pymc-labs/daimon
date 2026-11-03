import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Zap } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import type { Routine } from "./types"

/** Makes a routine due now; the scheduler runs it within a minute. */
export function RunNow({ routine: r }: { routine: Routine }) {
  const qc = useQueryClient()
  const run = useMutation({
    mutationFn: () => api.post<Routine>(`/api/routines/${r.id}/run`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["routines"] }); toast("Queued to run") },
  })
  return (
    <Button variant="outline" size="sm" disabled={!r.enabled || run.isPending} onClick={() => run.mutate()}>
      <Zap />Run now
    </Button>
  )
}

import { Trash2 } from "lucide-react"

import { Ago } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { useConfirm } from "@/lib/confirm"
import { useClearState, useSavedState } from "./api"
import { JsonTree } from "./run/parts"
import { Part } from "./tabparts"
import type { Workflow } from "./types"

/** What earlier runs saved with a Finish step, as the next run reads it. */
export function StateTab({ wf }: { wf: Workflow }) {
  const query = useSavedState(wf.id)
  const clear = useClearState(wf.id)
  const confirm = useConfirm()
  const empty = !Object.keys(query.data?.state ?? {}).length
  const wipe = async () => {
    if (await confirm({ title: "Clear the saved state?", action: "Clear", destructive: true })) clear.mutate()
  }
  return (
    <QueryState query={query}>
      {(s) => (
        <Part title="Saved state" aside={s.updated_at && <span className="text-xs text-muted-foreground">Saved <Ago iso={s.updated_at} /></span>}>
          {empty ? <p className="text-sm text-faint">Nothing saved</p> : <JsonTree value={s.state} root="state" />}
          {wf.can_edit && !empty && (
            <div className="mt-4 flex justify-end">
              <Button variant="outline" size="sm" disabled={clear.isPending} onClick={() => void wipe()}><Trash2 />Clear</Button>
            </div>
          )}
        </Part>
      )}
    </QueryState>
  )
}

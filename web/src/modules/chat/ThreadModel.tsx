import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, ChevronDown } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { api } from "@/lib/api"
import type { ModelPreview } from "../agents/types"
import { ModelPreviewPanel } from "../agents/ModelPreviewPanel"
import type { ThreadModels } from "./types"

const DEFAULT = "default"
type Pending = { value: string; label: string; preview: ModelPreview }

/** The chat's model: pick one for this chat only; a pick that moves provider shows what changes and asks once. */
export function ThreadModel({ sid, model, onChanged }: { sid: string; model: string; onChanged: () => void }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState<Pending | null>(null)
  const [busy, setBusy] = useState(false)
  const models = useQuery({
    queryKey: ["chat", "session", sid, "models"],
    queryFn: () => api.get<ThreadModels>(`/api/chat/sessions/${sid}/models`),
    enabled: open,
    retry: false,
  })

  const keep = async (value: string, preview?: ModelPreview) => {
    setBusy(true)
    try {
      const done = await api.put<{ note: string }>(`/api/chat/sessions/${sid}/model`, {
        value, preview_digest: preview?.digest ?? "", preview_revision: preview?.revision ?? 0,
      })
      setPending(null)
      void qc.invalidateQueries({ queryKey: ["chat", "session", sid] })
      onChanged()
      toast.success(done.note)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Couldn't change the model.")
    } finally { setBusy(false) }
  }

  const pick = async (value: string, label: string) => {
    if (value === DEFAULT) return keep(value)
    try {
      const preview = await api.get<ModelPreview>(`/api/chat/sessions/${sid}/model-preview?value=${encodeURIComponent(value)}`)
      if (preview.moves || preview.refusal) setPending({ value, label, preview })
      else await keep(value)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Couldn't check that model.")
    }
  }

  const refused = !!pending?.preview.refusal || pending?.preview.allowed === false
  return (
    <>
      <DropdownMenu open={open} onOpenChange={setOpen}>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="xs" className="text-faint" aria-label="Chat model">
            {model || "Model"}<ChevronDown />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
          <DropdownMenuItem onSelect={() => void pick(DEFAULT, "the agent's own model")}>
            Agent default{models.data?.current == null && <Check className="ml-auto" />}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          {models.isPending && <div className="px-2 py-1.5 text-sm text-muted-foreground">Loading…</div>}
          {models.data?.choices.map((c) => (
            <DropdownMenuItem key={c.value} onSelect={() => void pick(c.value, c.label)}>
              <span>{c.label}</span>
              <span className="ml-2 text-xs text-muted-foreground">{c.price}</span>
              {models.data.current === c.value && <Check className="ml-auto" />}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog open={!!pending} onOpenChange={(o) => { if (!o) setPending(null) }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader><DialogTitle>Switch this chat to {pending?.label}?</DialogTitle></DialogHeader>
          {pending && <ModelPreviewPanel preview={pending.preview} />}
          <DialogFooter>
            <Button variant="outline" onClick={() => setPending(null)}>Cancel</Button>
            <Button disabled={busy || refused} onClick={() => pending && void keep(pending.value, pending.preview)}>Switch</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

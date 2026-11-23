import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, ChevronDown } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { api } from "@/lib/api"
import type { ThreadModels } from "./types"

const DEFAULT = "default"

/** The chat's model: a pick applies to this chat at once; its note says what is off on the new provider. */
export function ThreadModel({ sid, model }: { sid: string; model: string }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const models = useQuery({
    queryKey: ["chat", "session", sid, "models"],
    queryFn: () => api.get<ThreadModels>(`/api/chat/sessions/${sid}/models`),
    enabled: open,
    retry: false,
  })

  const pick = async (value: string) => {
    try {
      const done = await api.put<{ note: string }>(`/api/chat/sessions/${sid}/model`, { value })
      // The header reads the model from the thread list first, so both refetch.
      void qc.invalidateQueries({ queryKey: ["chat", "session", sid] })
      void qc.invalidateQueries({ queryKey: ["chat", "sessions"] })
      toast.success(done.note)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Couldn't change the model.")
    }
  }

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="xs" className="min-w-0 shrink text-faint" aria-label="Chat model">
          <span className="truncate">{model || "Model"}</span><ChevronDown />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
        <DropdownMenuItem onSelect={() => void pick(DEFAULT)}>
          Agent default{models.data?.current == null && <Check className="ml-auto" />}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {models.isPending && <div className="px-2 py-1.5 text-sm text-muted-foreground">Loading…</div>}
        {models.data?.choices.map((c) => (
          <DropdownMenuItem key={c.value} onSelect={() => void pick(c.value)}>
            <span>{c.label}</span>
            <span className="ml-2 text-xs text-muted-foreground">{c.price}</span>
            {models.data.current === c.value && <Check className="ml-auto" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

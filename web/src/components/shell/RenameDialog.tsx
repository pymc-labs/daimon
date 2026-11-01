import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import type { ChatSession } from "@/lib/types"

export function RenameDialog({ session, onClose }: { session: ChatSession | null; onClose: () => void }) {
  const [title, setTitle] = useState("")
  const qc = useQueryClient()
  useEffect(() => setTitle(session?.title ?? ""), [session])
  const save = useMutation({
    mutationFn: () => api.patch(`/api/chat/sessions/${session!.id}`, { title: title.trim() }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["chat"] }); onClose() },
  })
  return (
    <Dialog open={!!session} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Rename chat</DialogTitle></DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); if (title.trim()) save.mutate() }} className="space-y-4">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus maxLength={120} />
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={!title.trim() || save.isPending}>Save</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

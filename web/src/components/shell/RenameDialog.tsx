import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import type { ChatSession } from "@/lib/types"

export function RenameDialog({ session, onClose }: { session: ChatSession | null; onClose: () => void }) {
  return (
    <Dialog open={!!session} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Rename chat</DialogTitle></DialogHeader>
        {/* Keyed by session, so the draft title starts from that session's title each time it opens. */}
        <RenameForm key={session?.id} session={session} onClose={onClose} />
      </DialogContent>
    </Dialog>
  )
}

function RenameForm({ session, onClose }: { session: ChatSession | null; onClose: () => void }) {
  const [title, setTitle] = useState(session?.title ?? "")
  const qc = useQueryClient()
  const save = useMutation({
    mutationFn: () => api.patch(`/api/chat/sessions/${session!.id}`, { title: title.trim() }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["chat"] }); onClose() },
  })
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (title.trim()) save.mutate() }} className="space-y-4">
      <Input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus maxLength={120} />
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={!title.trim() || save.isPending}>Save</Button>
      </DialogFooter>
    </form>
  )
}

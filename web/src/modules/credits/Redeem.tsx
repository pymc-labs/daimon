import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { api } from "@/lib/api"
import { shortDate, usd } from "@/lib/format"
import type { Redeemed } from "./types"

export function Redeem({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [code, setCode] = useState("")
  const qc = useQueryClient()
  const go = useMutation({
    mutationFn: () => api.post<Redeemed>("/api/credits/redeem", { code: code.trim() }),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["credits"] }); void qc.invalidateQueries({ queryKey: ["me"] })
      setCode(""); onOpenChange(false)
      toast(r.starts_at ? `${usd(r.amount_usd)} starts ${shortDate(r.starts_at)}` : `Added ${usd(r.amount_usd)}`)
    },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Redeem a code</DialogTitle></DialogHeader>
        <form id="redeem" className="space-y-1.5" onSubmit={(e) => { e.preventDefault(); if (code.trim()) go.mutate() }}>
          <Label htmlFor="code">Code</Label>
          <Input id="code" autoComplete="off" autoCapitalize="characters" value={code} onChange={(e) => setCode(e.target.value)} />
        </form>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="redeem" disabled={!code.trim() || go.isPending}>Redeem</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
